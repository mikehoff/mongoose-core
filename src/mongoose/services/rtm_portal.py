"""RTM Leaseholder Portal: Mongoose side.

Snapshot-push architecture: Mongoose is the ONLY source of truth for flats, demands,
ledger entries, balances and PDFs. WordPress is the public display surface only.
See spec: docs/RTM_PORTAL_SPEC.md for the full design.

Status: IMPLEMENTED. Claims are decrypted locally with the NaCl private key (PyNaCl);
WordPress only ever holds the public key and encrypted payloads.
The scheduler calls pull_pending_claims + process_pending_claims on a slow cadence;
brain tools expose readiness and snapshot; the CLI exposes all eight management commands.

NON-GOALS (enforced in snapshot layer):
  - No card payments, no automatic ledger credits, no debt recovery automation.
  - No supplier payables or director-only evidence in the snapshot.
  - No FTT strategy, witness statements or without-prejudice material ever leaves Mongoose.

SECURITY:
  - Claim validation is ONLY done here. WordPress never sees claim hashes, HMAC pepper,
    or raw payment-reference/document-number pairs in persistent storage.
  - Claims arrive encrypted (libsodium sealed box using our public key).
  - Flat-level statement data is only pushed for claimed+active flats by default.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import profile as _profile
from ..settings import site_domain as _site_domain

# ── Constants ─────────────────────────────────────────────────────────────────

PORTAL_VERSION = "1.0.0"
# The snapshot SCHEMA version WordPress validates: it must be an INTEGER (WP rejects a non-integer with
# "version is not an integer", and rejects a value newer than pc_rtm_snapshot_schema_max). The semver
# PORTAL_VERSION string above is for human/readiness display only; the wire snapshot must carry this int,
# or every publish silently 400s and WordPress freezes on its last-known-good.
PORTAL_SNAPSHOT_SCHEMA_VERSION = 1

# Exact wording required by spec: never alter these strings.
CAVEAT_7DAY = (
    "Payment records are reconciled manually from the RTM bank account. "
    "The portal can take up to 7 days to be fully up to date after you make a payment. "
    "If you paid more than 7 days ago and it is not shown, raise a payment issue and "
    "include the payment date, amount and reference used."
)
PAYMENT_INSTRUCTION = (
    "Please use the reference exactly as shown so your payment can be matched."
)
CLAIM_FAILURE_MESSAGE = (
    "We could not match those details. Please check your payment reference and "
    "document number exactly as shown on your demand."
)

# Account states (spec §5.3)
ACCOUNT_STATES = frozenset({
    "pending_claim",
    "pending_email_verification",
    "pending_director_review",
    "active",
    "rejected",
    "disabled",
})

# Data fields NEVER allowed in any portal snapshot (spec §8)
_SNAPSHOT_EXCLUSIONS = frozenset({
    "ftt_strategy", "witness_statement", "skeleton_argument",
    "without_prejudice", "director_notes", "supplier_payables",
    "contractor_quotes", "internal_evidence", "legal_advice",
    "mongoose_audit", "raw_bank_transaction", "raw_claim_combination",
    "claim_hash", "hmac_pepper", "claim_secret",
})


# ── Settings helpers ──────────────────────────────────────────────────────────

def _portal_env() -> dict[str, str]:
    """Read portal-specific env vars (from .env via process environment)."""
    keys = [
        "RTM_PORTAL_BANK_NAME", "RTM_PORTAL_ACCOUNT_NAME",
        "RTM_PORTAL_SORT_CODE", "RTM_PORTAL_ACCOUNT_NUMBER",
        "RTM_PORTAL_PAYMENT_INSTRUCTIONS",
        "PC_PORTAL_PRIVATE_DIR",
        "PC_PORTAL_CLAIM_KEYPAIR_PATH",
        "WP_URL", "WP_USERNAME", "WP_APP_PASSWORD",
        "MONGOOSE_WEBHOOK_SECRET",
    ]
    return {k: os.environ.get(k, "") for k in keys}


def _bank_details_ok(env: dict[str, str] | None = None) -> bool:
    """True when the portal can safely show payment details: either explicit RTM_PORTAL_* env overrides
    are all set, OR the canonical bank block (already printed on every demand) is available as fallback."""
    env = env or _portal_env()
    required = ["RTM_PORTAL_BANK_NAME", "RTM_PORTAL_ACCOUNT_NAME",
                "RTM_PORTAL_SORT_CODE", "RTM_PORTAL_ACCOUNT_NUMBER"]
    if all(env.get(k, "").strip() for k in required):
        return True
    return _resolve_bank_details(env, _allow_recurse=False) is not None


def _resolve_bank_details(env: dict[str, str] | None = None, *, _allow_recurse: bool = True) -> dict[str, str] | None:
    """The bank block to PUBLISH. Prefer explicit RTM_PORTAL_* env overrides; otherwise fall back to the
    canonical block already on every demand + cover email (rtm_templates.BRANDING), so the portal exposes
    the SAME public details as the demands instead of hiding them behind a 'being checked' veto."""
    env = env or _portal_env()
    req = ["RTM_PORTAL_BANK_NAME", "RTM_PORTAL_ACCOUNT_NAME", "RTM_PORTAL_SORT_CODE", "RTM_PORTAL_ACCOUNT_NUMBER"]
    if all(env.get(k, "").strip() for k in req):
        return {"bank_name": env["RTM_PORTAL_BANK_NAME"].strip(),
                "account_name": env["RTM_PORTAL_ACCOUNT_NAME"].strip(),
                "sort_code": env["RTM_PORTAL_SORT_CODE"].strip(),
                "account_number": env["RTM_PORTAL_ACCOUNT_NUMBER"].strip()}
    try:
        from .rtm_templates import BRANDING as _B
        if str(_B.get("bank_sort_code", "")).strip() and str(_B.get("bank_account_number", "")).strip():
            return {"bank_name": _B.get("bank_name", ""),
                    "account_name": _B.get("bank_account_name", ""),
                    "sort_code": _B.get("bank_sort_code", ""),
                    "account_number": _B.get("bank_account_number", "")}
    except Exception:
        pass
    return None


def _wp_configured(env: dict[str, str] | None = None) -> bool:
    env = env or _portal_env()
    return bool(env.get("WP_URL") and env.get("WP_USERNAME") and env.get("WP_APP_PASSWORD"))




# ── Crypto (PyNaCl guarded) ──────────────────────────────────────────────────

def _nacl():
    """Import PyNaCl lazily. Returns the nacl.public module or None with a clear reason."""
    try:
        import nacl.public as npub  # type: ignore
        return npub
    except Exception:
        return None


def _keypair_path(env: dict[str, str] | None = None) -> Path:
    env = env or _portal_env()
    p = env.get("PC_PORTAL_CLAIM_KEYPAIR_PATH", "").strip()
    if p:
        return Path(p).expanduser()
    data_dir = (os.environ.get("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    return Path(data_dir) / "portal" / "claim_keypair.json"


def generate_claim_keypair() -> dict[str, Any]:
    """Generate the NaCl keypair for portal claim encryption.

    WordPress gets only the PUBLIC key (sodium_crypto_box_seal on the PHP side).
    The private key never leaves this machine; the file is chmod 600."""
    npub = _nacl()
    if npub is None:
        return {"ok": False, "error": "PyNaCl is not installed. Run: pip install pynacl"}
    import base64
    import json as _json
    path = _keypair_path()
    if path.exists():
        return {"ok": False, "error": f"keypair already exists at {path}; delete it deliberately to rotate",
                "keypair_path": str(path)}
    key = npub.PrivateKey.generate()
    payload = {
        "algorithm": "x25519-xsalsa20-poly1305",
        "private_b64": base64.b64encode(bytes(key)).decode(),
        "public_b64": base64.b64encode(bytes(key.public_key)).decode(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return {"ok": True, "public_key_b64": payload["public_b64"], "keypair_path": str(path),
            "algorithm": payload["algorithm"]}


def _load_keypair() -> dict[str, Any] | None:
    import json as _json
    path = _keypair_path()
    if not path.exists():
        return None
    try:
        d = _json.loads(path.read_text())
        return d if d.get("private_b64") and d.get("public_b64") else None
    except Exception:
        return None


def get_claim_public_key() -> dict[str, Any]:
    """Return the base64 public key to publish to WordPress during portal setup."""
    kp = _load_keypair()
    if not kp:
        return {"ok": False, "error": "no keypair; run: mongoose rtm portal generate-keypair"}
    return {"ok": True, "public_key_b64": kp["public_b64"],
            "algorithm": kp.get("algorithm", "x25519-xsalsa20-poly1305")}


# ---------------------------------------------------------------------------------------------------------
# Snapshot SIGNING key (Ed25519). Asymmetric authentication of everything Mongoose WRITES to WordPress:
# Mongoose signs with the PRIVATE key (never leaves the Mac), the plugin verifies with the PUBLIC key. This
# replaces the symmetric HMAC whose secret was stored inside WordPress, so a fully-compromised WordPress can
# read every option and STILL cannot forge a push. This is the fix for the 2026-07 breach.
# ---------------------------------------------------------------------------------------------------------

def _signing_key_path() -> Path:
    p = (_portal_env().get("PC_PORTAL_SIGNING_KEY_PATH", "") or "").strip()
    if p:
        return Path(p).expanduser()
    data_dir = (os.environ.get("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    return Path(data_dir) / "portal" / "snapshot_signing_key.json"


def _nacl_signing():
    try:
        import nacl.signing as nsign  # type: ignore
        return nsign
    except Exception:
        return None


def generate_snapshot_signing_key() -> dict[str, Any]:
    """Generate the Ed25519 signing keypair. WordPress gets only the PUBLIC (verify) key; the private
    (signing) key stays on this machine, chmod 600, and signs every write to the portal."""
    nsign = _nacl_signing()
    if nsign is None:
        return {"ok": False, "error": "PyNaCl not installed. Run: pip install pynacl"}
    import base64
    import json as _json
    path = _signing_key_path()
    if path.exists():
        return {"ok": False, "error": f"signing key already exists at {path}; delete deliberately to rotate",
                "path": str(path)}
    sk = nsign.SigningKey.generate()
    payload = {
        "algorithm": "ed25519",
        "private_b64": base64.b64encode(bytes(sk)).decode(),
        "public_b64": base64.b64encode(bytes(sk.verify_key)).decode(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(payload, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return {"ok": True, "public_key_b64": payload["public_b64"], "path": str(path)}


def get_snapshot_signing_public_key() -> dict[str, Any]:
    """The base64 Ed25519 verify key to paste into the WordPress RTM Settings (pc_rtm_snapshot_pubkey)."""
    import json as _json
    path = _signing_key_path()
    if not path.exists():
        return {"ok": False, "error": "no signing key; run: mongoose rtm portal generate-signing-key"}
    try:
        d = _json.loads(path.read_text())
        return {"ok": True, "public_key_b64": d.get("public_b64", ""), "algorithm": "ed25519"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _security_hold_path() -> Path:
    data_dir = (os.environ.get("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    return Path(data_dir) / "portal" / "SECURITY_HOLD"


def _engage_security_hold(reason: str) -> None:
    """Durably pause all portal traffic by dropping the hold marker (checked in _wp_call). Idempotent."""
    try:
        p = _security_hold_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{datetime.now(timezone.utc).isoformat()}\n{reason}\n")
    except Exception:
        pass


def check_snapshot_drift() -> dict[str, Any]:
    """Ask WordPress what snapshot it is actually serving and compare its timestamp to the last one Mongoose
    published. If WordPress shows a snapshot NEWER than anything we pushed, something other than Mongoose
    wrote it: engage the durable security hold and alarm. This is the check that would have caught the
    2026-07 breach within minutes (WP said 07-18 14:47; our last publish was 07-17 15:32)."""
    res = _wp_call("GET", _profile.wp_route("/sync/snapshot-status"))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "checked": False}
    data = res.get("data") or {}
    # The plugin's snapshot-status route reports the timestamp as `snapshot_at`; older/blob builds used `at`.
    # Read both so drift detection is never silently blind to what WordPress is actually serving.
    wp_at = str(data.get("snapshot_at") or data.get("at") or "").strip()
    # Last snapshot Mongoose actually published (source of truth for "what we pushed").
    last_pub = ""
    try:
        from ..db import connect
        with connect() as con:
            row = con.execute(
                "SELECT created_at FROM portal_sync_runs WHERE kind='publish_snapshot' AND ok=1 "
                "ORDER BY created_at DESC LIMIT 1").fetchone()
            if row:
                last_pub = str(row[0] if isinstance(row, tuple) else row["created_at"])
    except Exception:
        pass

    def _parse(ts: str):
        ts = (ts or "").strip().replace(" ", "T")
        for cut in (19, 16):
            try:
                return datetime.fromisoformat(ts[:cut])
            except Exception:
                continue
        return None

    wp_dt, pub_dt = _parse(wp_at), _parse(last_pub)
    drift = bool(wp_dt and pub_dt and (wp_dt - pub_dt).total_seconds() > 300)  # 5-min slack for our own push
    unknown = bool(wp_at and not pub_dt)                                       # WP has data, we never published
    if drift or unknown:
        reason = (f"WordPress snapshot dated {wp_at} is newer than Mongoose's last publish ({last_pub or 'never'}); "
                  f"WordPress was written by something other than Mongoose.")
        _engage_security_hold(reason)
        _record_sync_run("drift_check", False, "DRIFT: " + reason)
        return {"ok": True, "drift": True, "held": True, "wp_at": wp_at, "last_publish": last_pub, "reason": reason}
    _record_sync_run("drift_check", True, f"clean (wp_at={wp_at}, last_publish={last_pub})")
    return {"ok": True, "drift": False, "wp_at": wp_at, "last_publish": last_pub}


def verify_receipt_deliveries(*, hours: int = 12) -> dict[str, Any]:
    """Independent delivery check: for every receipt Mongoose recorded as SENT, confirm the audit Bcc copy
    ACTUALLY landed in the info@ archive (which Mongoose IMAP-syncs). A send with no archive copy , once the
    mailbox sync has caught up PAST that send , is a real non-delivery the transport falsely reported as sent
    (for example WordPress reports ok but PHP mail() silently drops the message). Read-only; the
    scheduler alarms on 'unconfirmed'. Returns {confirmed, unconfirmed, pending}."""
    import re as _re
    from datetime import timedelta
    from ..db import connect
    out: dict[str, Any] = {"confirmed": [], "unconfirmed": [], "pending": []}
    seen: set[str] = set()
    with connect() as con:
        sync_high = con.execute("SELECT MAX(indexed_at) FROM email_messages").fetchone()[0] or ""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        runs = con.execute(
            "SELECT detail, created_at FROM portal_sync_runs WHERE kind='send_receipt' AND ok=1 "
            "AND created_at>=? ORDER BY created_at DESC", (cutoff,)).fetchall()
        for r in runs:
            detail = (r["detail"] if not isinstance(r, tuple) else r[0]) or ""
            sent_at = (r["created_at"] if not isinstance(r, tuple) else r[1]) or ""
            m = _re.match(r"\s*([0-9A-Za-z]+)\s*->", detail)
            if not m:
                continue
            fid = m.group(1).upper()
            if fid in seen:            # judge only the latest attempt per flat
                continue
            seen.add(fid)
            frow = con.execute(
                "SELECT leaseholder_email FROM rtm_mgmt_flats WHERE UPPER(REPLACE(flat_id,'-',''))=?", (fid,)).fetchone()
            email = (((frow["leaseholder_email"] if frow else "") or "")).strip().lower()
            rec = {"flat_id": fid, "email": email, "sent_at": sent_at[:19]}
            if not email:
                rec["reason"] = "no on-file email"
                out["unconfirmed"].append(rec)
                continue
            hit = con.execute(
                "SELECT 1 FROM email_messages WHERE lower(subject) LIKE '%receipt%' "
                "AND lower(recipients) LIKE ? AND indexed_at>=? LIMIT 1", ("%" + email + "%", sent_at)).fetchone()
            if hit:
                out["confirmed"].append(rec)
            elif sync_high and sync_high > sent_at:   # sync ran PAST the send, still no copy -> real miss
                rec["reason"] = "no audit copy in info@ archive despite mailbox sync catching up"
                out["unconfirmed"].append(rec)
            else:
                out["pending"].append(rec)            # sync not caught up yet; re-judged next cycle
    return out


def build_payment_plan_letter(flat_id: str, *, erratum: str = "") -> dict[str, Any]:
    """Compose the payment-plan letter WITHOUT sending it, so the owner can read it first.

    Deliberately refuses rather than guessing: a letter that greets a human by their internal flat id
    ("Dear 3ECF3") or omits how to pay is worse than no letter. `erratum`, when given, is rendered as a
    prominent red notice at the top (used to retract a letter sent in error).
    """
    from html import escape as _esc
    from datetime import date as _date, timedelta as _timedelta
    from .rtm_payment_plans import plan_progress, _money, _today_iso
    from .rtm_templates import BRANDING, premises
    from . import rtm_management as M

    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    # plan_progress, not payment_plan_for: the letter must know what has ALREADY been paid. Composing from
    # the bare agreed schedule is what made this letter unsendable once a plan was running - it restated
    # month one as though nothing had happened and quoted a total the leaseholder had already part-paid.
    plan = plan_progress(fid)
    if not plan:
        return {"ok": False, "error": f"no agreed plan for {fid}"}
    # The NAME comes from the flat record (flat_statement carries balances only, which is exactly how
    # every letter came to be addressed "Dear <FLATID>"). Prefer the director's curated greeting_name.
    flat = M._get_flat(fid) or {}
    greeting = str(flat.get("greeting_name") or "").strip() or str(flat.get("leaseholder_name") or "").strip()
    if not greeting:
        return {"ok": False, "error": (
            f"{fid} has no leaseholder name or greeting on file; refusing to address a payment-plan "
            "letter to a flat id. Set the name first.")}
    ref = str(flat.get("payment_reference") or "").strip()
    if not ref:
        return {"ok": False, "error": f"{fid} has no payment reference; refusing to send pay-by instructions"}

    def _long(iso: str) -> str:
        try:
            d = _date.fromisoformat(iso[:10])
            return f"{d.day} {d.strftime('%B %Y')}"
        except Exception:
            return iso

    inst = plan["instalments"]
    so_amount = inst[0]["amount"] if inst else ""
    so_day = ""
    try:
        so_day = str(_date.fromisoformat(inst[0]["due_date"][:10]).day)
    except Exception:
        pass
    bank = (f"{BRANDING['bank_account_name']}, {BRANDING['bank_name']}, sort code "
            f"{BRANDING['bank_sort_code']}, account {BRANDING['bank_account_number']}, reference {ref}")

    # ── Position as at today ────────────────────────────────────────────────────────────────────────
    # A plan letter is written at a moment in time, and after the first due date the agreed schedule alone
    # stops being a true statement: some instalments are paid, and any unpaid one whose date has passed is
    # payable NOW, not on a date already gone. Rather than print a row of past dates and leave the
    # leaseholder to work out the arithmetic, sum every instalment that has fallen due and is unpaid into
    # ONE catch-up figure payable on receipt, and list only what is genuinely still ahead of them.
    today = _today_iso()
    # A deadline the leaseholder can act on. "On receipt" is unenforceable and unmeasurable; a dated
    # 7-day window is both, and it is the same window the chaser process assumes.
    try:
        _pay_by = (_date.fromisoformat(today) + _timedelta(days=7)).isoformat()
    except Exception:
        _pay_by = today
    sched = plan["schedule"]
    settled = [i for i in sched if i["paid"]]
    payable_now = [i for i in sched if not i["paid"] and str(i["due_date"]) <= today]
    upcoming = [i for i in sched if not i["paid"] and str(i["due_date"]) > today]
    now_pence = sum(int(i["amount_pence"]) for i in payable_now)
    settled_pence = sum(int(i["paid_amount_pence"]) for i in settled)

    def _nlist(items: list) -> str:
        ns = [str(int(i["n"])) for i in items]
        if len(ns) <= 1:
            return ns[0] if ns else ""
        return ", ".join(ns[:-1]) + " and " + ns[-1]

    top = ""
    if erratum:
        top = ("<p style=\"color:#b32d2e;font-weight:700;border:2px solid #b32d2e;padding:10px\">"
               + _esc(erratum) + "</p>")

    if not settled and not payable_now:
        # A plan agreed today, nothing due yet: the schedule IS the whole story, so say it plainly.
        lines = "".join(f"<li>{_esc(_long(i['due_date']))}: {_esc(i['amount'])}</li>" for i in inst)
        middle = (
            f"<p>We agree the following service charge payment plan for "
            f"{_esc(premises(fid[:3], fid[4:] if len(fid) > 4 else ''))}: "
            f"{_esc(plan['total'])} over {plan['months']} monthly instalments.</p>"
            f"<ul>{lines}</ul>")
    else:
        parts = [
            f"<p>We agree the following service charge payment plan for "
            f"{_esc(premises(fid[:3], fid[4:] if len(fid) > 4 else ''))}: "
            f"{_esc(plan['total'])} over {plan['months']} monthly instalments.</p>"]
        if settled:
            parts.append(
                f"<p>We have received {len(settled)} of {plan['months']} instalments, "
                f"totalling {_esc(_money(settled_pence))}, for which we thank you.</p>")
        if payable_now:
            word = "instalment" if len(payable_now) == 1 else "instalments"
            parts.append(
                f"<p><strong>{_esc(_money(now_pence))} is payable now</strong>, being {word} "
                f"{_esc(_nlist(payable_now))} of {plan['months']} "
                f"({'due ' + _esc(_long(payable_now[0]['due_date'])) if len(payable_now) == 1 else 'due between ' + _esc(_long(payable_now[0]['due_date'])) + ' and ' + _esc(_long(payable_now[-1]['due_date']))}). "
                f"Please pay this in one sum <strong>within 7 days of the date of this letter "
                f"({_esc(_long(_pay_by))})</strong>.</p>")
        if upcoming:
            ul = "".join(f"<li>{_esc(_long(i['due_date']))}: {_esc(i['amount'])}</li>" for i in upcoming)
            parts.append(f"<p>Your remaining instalments are then:</p><ul>{ul}</ul>")
        else:
            parts.append("<p>No further instalments fall due after this payment, which clears the plan "
                         "in full.</p>")
        # Placed after the lump sum and the remaining list, a bare "that leaves X outstanding" reads as
        # the balance AFTER the lump sum, which it is not - it is the whole of what is still owed,
        # lump sum included. Say which, so the two figures cannot be added together by mistake.
        if payable_now:
            parts.append(f"<p>The payment above and the remaining instalments together come to "
                         f"{_esc(plan['outstanding'])}, which is the full amount outstanding on the plan.</p>")
        else:
            parts.append(f"<p>That leaves {_esc(plan['outstanding'])} outstanding on the plan in total.</p>")
        middle = "".join(parts)

    if upcoming and payable_now:
        # After a catch-up the leaseholder should not have to ask what happens next: a standing order for
        # the ordinary instalment, from the next due date, settles the rest of the plan.
        so_note = (f"<p>For the instalments that follow, a standing order for {_esc(so_amount)} on the "
                   f"{_esc(so_day)}th of each month is acceptable, payable to {_esc(bank)}. The lump sum "
                   f"above is separate from, and in addition to, that standing order.</p>")
    elif upcoming:
        so_note = (f"<p>Please set up a standing order for {_esc(so_amount)} on the {_esc(so_day)}th of "
                   f"each month to {_esc(bank)}.</p>")
    else:
        so_note = f"<p>Payment should be made to {_esc(bank)}.</p>"

    body_html = (
        f"{top}"
        f"<p>Dear {_esc(greeting)},</p>"
        f"{middle}"
        f"{so_note}"
        f"<p>As this is a payment plan against the demand, not a change to your lease, if an instalment "
        f"is missed the outstanding balance becomes payable in full. If you are unable to meet an "
        f"instalment, please contact us before the due date so we can help.</p>")
    return {"ok": True, "flat_id": fid, "greeting": greeting,
            "subject": f"Your {_profile.rtm_name()} service charge payment plan",
            "body_html": body_html, "instalments": inst, "total": plan["total"],
            "as_at": today, "pay_by": _pay_by,
            "position": {
                "settled_count": len(settled), "settled": _money(settled_pence),
                "payable_now": _money(now_pence), "payable_now_pence": now_pence,
                "payable_now_instalments": [int(i["n"]) for i in payable_now],
                "upcoming_count": len(upcoming), "outstanding": plan["outstanding"],
            },
            "standing_order": {"amount": so_amount, "day": so_day, "bank": bank}}


def send_payment_plan_letter(flat_id: str, *, erratum: str = "", confirm: bool = False) -> dict[str, Any]:
    """Email the leaseholder their payment-plan letter via /portal/send-letter (plugin SMTP + audit Bcc
    + delivery verification).

    CONFIRM-GATED: this used to fire automatically off a WordPress button, so a wrong schedule reached a
    real leaseholder with nobody having read it. Pass confirm=True only after a human has seen the draft
    (build_payment_plan_letter).
    """
    draft = build_payment_plan_letter(flat_id, erratum=erratum)
    if not draft.get("ok"):
        return draft
    if not confirm:
        return {**draft, "sent": False, "reason": "confirm_required",
                "note": "Draft only. Re-run with confirm=True to email the leaseholder."}
    # Copy the same parties as any other correspondence to this flat (managing agent + alt addresses).
    # The letter used to copy nobody, so a leaseholder whose agent was on the demand lost them here.
    try:
        from .rtm_management import correspondence_cc
        _cc = correspondence_cc(draft["flat_id"])
    except Exception:
        _cc = []
    body = {"flat_id": draft["flat_id"], "subject": draft["subject"], "body_html": draft["body_html"],
            "cc": ", ".join(_cc)}
    res = _wp_call("POST", _profile.wp_route("/portal/send-letter"), json_body=body, timeout=60.0)
    _record_sync_run("send_plan_letter", bool(res.get("ok")), res.get("error") or f"{draft['flat_id']}")
    return {"ok": bool(res.get("ok")), "sent": bool(res.get("ok")), "flat_id": draft["flat_id"],
            "error": res.get("error")}


LETTER_COVER_HTML = ("<p style=\"margin:0 0 14px\">Please find important communication attached from your management "
                     "company.</p><p style=\"margin:0\">Director, Example Court RTM</p>")


def send_attached_letter(flat_id: str, pdf_path: str, *, subject: str, document_number: str,
                         attachment_name: str = "", body_html: str = LETTER_COVER_HTML, test: bool = False,
                         confirm: bool = False) -> dict[str, Any]:
    """Email a leaseholder a letter as a PDF attachment through the portal (info@example.org, SMTP,
    audit Bcc, Sent mail log). The PDF goes into the flat-gated store first, then /portal/send-letter
    attaches it by key AND flat, so it can only ever reach the flat it was written for.

    test=True sends only to the RTM's own address, copying nobody. A real send is CONFIRM-GATED: nothing
    reaches a leaseholder unless confirm=True, which should follow a human reading the test."""
    import base64 as _b64
    import hashlib
    import secrets
    flat_id = (flat_id or "").strip().upper()
    path = Path(pdf_path).expanduser()
    if not flat_id or not path.exists():
        return {"ok": False, "error": f"need a flat and an existing PDF ({path})"}
    raw = path.read_bytes()
    if raw[:4] != b"%PDF":
        return {"ok": False, "error": "not a PDF"}
    if not test and not confirm:
        return {"ok": False, "sent": False, "reason": "confirm_required", "flat_id": flat_id,
                "note": "Real sends need confirm=True after the test has been read."}
    pdf_key = "letter-" + secrets.token_urlsafe(18)
    push = _wp_call("POST", "/wp-json/example-court/v1/sync/demand-pdf", json_body={
        "pdf_key": pdf_key, "document_number": document_number, "flat_id": flat_id,
        "content_b64": _b64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest()}, timeout=60.0)
    if not push.get("ok"):
        return {"ok": False, "error": f"could not store the letter in the portal: {push.get('error')}", "flat_id": flat_id}
    try:
        from .rtm_management import correspondence_cc
        cc = [] if test else correspondence_cc(flat_id)
    except Exception:
        cc = []
    body = {"flat_id": flat_id, "subject": subject, "body_html": body_html, "pdf_key": pdf_key,
            "attachment_name": attachment_name or path.name, "signoff": False, "test": bool(test),
            "cc": ", ".join(cc)}
    res = _wp_call("POST", "/wp-json/example-court/v1/portal/send-letter", json_body=body, timeout=90.0)
    _record_sync_run("send_attached_letter" + ("_test" if test else ""), bool(res.get("ok")),
                     res.get("error") or f"{flat_id} {document_number} -> {res.get('email', '')}")
    if res.get("ok") and not test and not res.get("attached"):
        # An old plugin ignores pdf_key and sends the cover note alone: never report that as a letter sent.
        return {"ok": False, "error": "the portal sent the email WITHOUT the attachment (plugin older than 1.1.14)",
                "flat_id": flat_id, "email": res.get("email")}
    return {"ok": bool(res.get("ok")), "sent": bool(res.get("ok")), "test": bool(test), "flat_id": flat_id,
            "email": res.get("email"), "attached": res.get("attached"), "cc": cc, "error": res.get("error")}


def _sign_body(content: bytes) -> str:
    """Detached Ed25519 signature over the request body, base64. '' if no key/nacl (then HMAC still applies)."""
    nsign = _nacl_signing()
    if nsign is None:
        return ""
    import base64
    import json as _json
    path = _signing_key_path()
    if not path.exists():
        return ""
    try:
        priv = base64.b64decode(_json.loads(path.read_text())["private_b64"])
        sig = nsign.SigningKey(priv).sign(content).signature
        return base64.b64encode(sig).decode()
    except Exception:
        return ""


def decrypt_claim_payload(encrypted_b64: str) -> dict[str, Any]:
    """Decrypt a sealed-box claim payload with the local private key. Returns parsed JSON.

    Never logs the decrypted content."""
    npub = _nacl()
    if npub is None:
        return {"ok": False, "error": "PyNaCl is not installed"}
    kp = _load_keypair()
    if not kp:
        return {"ok": False, "error": "no keypair"}
    import base64
    import json as _json
    try:
        priv = npub.PrivateKey(base64.b64decode(kp["private_b64"]))
        box = npub.SealedBox(priv)
        raw = box.decrypt(base64.b64decode(encrypted_b64))
        data = _json.loads(raw.decode("utf-8"))
        return {"ok": True, "claim": data}
    except Exception:
        # Deliberately generic: never echo ciphertext or partial plaintext into logs.
        return {"ok": False, "error": "decryption failed"}


# ── Local portal tables ───────────────────────────────────────────────────────

def _ensure_portal_tables() -> None:
    from ..db import connect
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS portal_demand_pdfs (
                document_number TEXT PRIMARY KEY,
                pdf_key TEXT NOT NULL,
                pdf_sha256 TEXT NOT NULL,
                pdf_storage_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portal_receipt_pdfs (
                receipt_no TEXT PRIMARY KEY,
                flat_id TEXT NOT NULL,
                pdf_key TEXT NOT NULL,
                pdf_sha256 TEXT NOT NULL,
                pdf_storage_path TEXT NOT NULL,
                period TEXT NOT NULL DEFAULT '',
                amount TEXT NOT NULL DEFAULT '',
                received_date TEXT NOT NULL DEFAULT '',
                document_number TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS portal_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                ok INTEGER NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            -- Access requests that validated (correct payment ref + demand number) but could not be
            -- auto-verified by email, held here for the owner to approve or reject in the Cockpit. We
            -- persist the decrypted essentials at review time because WordPress no longer serves the
            -- claim once it leaves 'pending', so this is the only place to approve it from later.
            CREATE TABLE IF NOT EXISTS portal_review_claims (
                claim_id INTEGER PRIMARY KEY,
                flat_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                reason_code TEXT NOT NULL DEFAULT '',
                submitted_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            """
        )
        # Additive migration for installs created before receipts carried their demand. A receipt used
        # to record only flat_id + a prose period, so nothing tied it to the demand it paid and its key
        # (RCPT-<yy>-<flat>) repeated every period , with INSERT OR REPLACE that silently OVERWROTE the
        # previous period's receipt. Adding the column is safe and idempotent (no drop, no rewrite).
        cols = {r[1] for r in con.execute("PRAGMA table_info(portal_receipt_pdfs)").fetchall()}
        if "document_number" not in cols:
            con.execute("ALTER TABLE portal_receipt_pdfs ADD COLUMN document_number TEXT NOT NULL DEFAULT ''")
        con.commit()


def backfill_receipt_document_numbers(*, apply: bool = False) -> dict[str, Any]:
    """Stamp the demand's document_number onto historical receipts that predate the column.

    DRY-RUN by default. Only fills a receipt whose flat has an UNAMBIGUOUS demand (exactly one demand,
    or exactly one Paid demand); anything ambiguous is reported and left alone rather than guessed at,
    because attributing a payment receipt to the wrong demand is a financial error, not a display one.
    """
    from ..db import connect
    _ensure_portal_tables()
    filled: list[dict[str, str]] = []
    ambiguous: list[dict[str, Any]] = []
    already = 0
    with connect() as con:
        for r in con.execute("SELECT receipt_no, flat_id, document_number FROM portal_receipt_pdfs").fetchall():
            if (r["document_number"] or "").strip():
                already += 1
                continue
            try:
                ds = con.execute("SELECT document_number, status FROM rtm_mgmt_demands WHERE flat_id=?",
                                 (r["flat_id"],)).fetchall()
            except Exception:
                ds = []   # no demand ledger here: report as ambiguous rather than crash or invent one
            paid = [d for d in ds if (d["status"] or "") == "Paid"]
            pick = ds[0] if len(ds) == 1 else (paid[0] if len(paid) == 1 else None)
            if pick is None:
                ambiguous.append({"receipt_no": r["receipt_no"], "flat_id": r["flat_id"],
                                  "demands": len(ds), "paid": len(paid)})
                continue
            filled.append({"receipt_no": r["receipt_no"], "document_number": pick["document_number"]})
        if apply and filled:
            for f in filled:
                con.execute("UPDATE portal_receipt_pdfs SET document_number=? WHERE receipt_no=? AND document_number=''",
                            (f["document_number"], f["receipt_no"]))
            con.commit()
    return {"ok": True, "applied": bool(apply), "would_fill": len(filled), "already_stamped": already,
            "ambiguous": ambiguous, "details": filled}


def _record_sync_run(kind: str, ok: bool, detail: str = "") -> None:
    _ensure_portal_tables()
    from ..db import connect
    from ..db import utc_now_iso
    with connect() as con:
        con.execute("INSERT INTO portal_sync_runs(kind, ok, detail, created_at) VALUES(?,?,?,?)",
                    (kind, 1 if ok else 0, detail[:400], utc_now_iso()))
        con.commit()


def snapshot_publish_health(window: int = 20) -> dict[str, Any]:
    """Health of the WordPress snapshot publish, from recent portal_sync_runs of kind 'publish_snapshot'.
    A publish that keeps failing means WordPress is frozen on stale data (the exact silent-failure that hid
    the 'version is not an integer' bug), so the cockpit readiness and the phone alarm key on this."""
    _ensure_portal_tables()
    from ..db import connect
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT ok, detail, created_at FROM portal_sync_runs WHERE kind='publish_snapshot' "
            "ORDER BY id DESC LIMIT ?", (max(1, window),)).fetchall()]
    if not rows:
        return {"ok": True, "ever_run": False, "consecutive_failures": 0,
                "last_error": "", "last_ok_at": "", "last_at": ""}
    consecutive = 0
    for r in rows:
        if int(r["ok"]) == 0:
            consecutive += 1
        else:
            break
    last_ok = next((r["created_at"] for r in rows if int(r["ok"]) == 1), "")
    healthy = consecutive == 0
    return {"ok": healthy, "ever_run": True, "consecutive_failures": consecutive,
            "last_error": (rows[0]["detail"] if not healthy else ""),
            "last_ok_at": last_ok, "last_at": rows[0]["created_at"]}


def should_alarm_snapshot(dedup_hours: float = 6.0) -> str:
    """If the snapshot publish is failing and we have not alarmed in the last dedup_hours, RECORD an alarm
    marker and return the alarm message for the caller to push to the owner's phone. Returns '' when
    healthy or when an alarm went out recently (so a persistent failure pings once, not every sync cycle)."""
    h = snapshot_publish_health()
    if h["ok"]:
        return ""
    _ensure_portal_tables()
    from datetime import datetime, timezone, timedelta
    from ..db import connect
    with connect() as con:
        last = con.execute("SELECT created_at FROM portal_sync_runs WHERE kind='snapshot_alarm' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    if last:
        try:
            prev = datetime.fromisoformat(str(last["created_at"]).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - prev < timedelta(hours=dedup_hours):
                return ""
        except Exception:
            pass
    _record_sync_run("snapshot_alarm", False, str(h["last_error"])[:200])
    return (f"Portal snapshot publish is FAILING (x{h['consecutive_failures']}): {h['last_error']}. "
            f"WordPress is showing stale data until this is fixed (last OK {h['last_ok_at'] or 'never'}).")


# ── WordPress sync client ─────────────────────────────────────────────────────

def _wp_base() -> tuple[str, tuple[str, str]] | None:
    env = _portal_env()
    if not _wp_configured(env):
        return None
    return env["WP_URL"].rstrip("/"), (env["WP_USERNAME"], env["WP_APP_PASSWORD"])


def _hmac_signature(body: bytes) -> str:
    import hmac as _hmac
    secret = (_portal_env().get("MONGOOSE_WEBHOOK_SECRET") or "").encode()
    return _hmac.new(secret, body, hashlib.sha256).hexdigest() if secret else ""


@lru_cache(maxsize=1)
def _packaged_wp_namespace() -> str:
    import json as _json
    try:
        data = _json.loads(_profile.PACKAGED_PROFILE.read_text(encoding="utf-8"))
        return str(data["wordpress"]["namespace"]).strip("/")
    except Exception:
        return ""


def _resolve_wp_path(path: str) -> str:
    """Put a sync path on the active profile's REST namespace (see _wp_call)."""
    path = _profile.render(path or "")
    packaged, active = _packaged_wp_namespace(), _profile.wp_namespace()
    if packaged and packaged != active:
        prefix = f"/wp-json/{packaged}/"
        if path.startswith(prefix):
            path = f"/wp-json/{active}/" + path[len(prefix):]
    return path


def _wp_call(method: str, path: str, *, json_body: dict | None = None,
             timeout: float = 20.0) -> dict[str, Any]:
    """One call to the site plugin's sync API. Returns {ok, status, data|error}.

    `path` is a REST path on the site, normally built with profile.wp_route("/sync/...") so it carries the
    active profile's namespace. A `{{wordpress.rest}}` placeholder is rendered too, and a path still
    written against the packaged default profile's namespace is mapped onto the active one, so a caller
    that was not converted still reaches this site's plugin instead of a 404."""
    # Security kill-switch at the transport: MONGOOSE_PORTAL_SYNC=off refuses ALL WordPress traffic, push
    # AND pull, before any network call, so a possibly-compromised site can neither be written to nor
    # trusted for inbound data. This is the airtight choke point (every push/pull routes through here).
    import os as _os
    # The execution-context wall: a test/ephemeral/dev process may NEVER touch the live portal. This is the
    # boundary whose absence let test fixtures (Nina/Nadia/£300) reach the live leaseholder site on 07-18.
    from .egress_guard import egress_allowed
    _eg_ok, _eg_why = egress_allowed("wp_sync")
    if not _eg_ok:
        return {"ok": False, "error": f"egress blocked ({_eg_why}); not a live context"}
    if (_os.getenv("MONGOOSE_PORTAL_SYNC", "on") or "").strip().lower() == "off":
        return {"ok": False, "error": "portal sync paused (MONGOOSE_PORTAL_SYNC=off, security hold)"}
    # Durable auto-pause: drift detection drops this marker the instant WordPress shows a snapshot Mongoose
    # did not push. It survives a restart (unlike an env var), so a detected tamper stays contained until a
    # human clears it deliberately.
    hold = _security_hold_path()
    if hold.exists():
        return {"ok": False, "error": f"portal sync paused (security hold: {hold}). Clear it deliberately to resume."}
    base = _wp_base()
    if not base:
        return {"ok": False, "error": "WordPress not configured (WP_URL/WP_USERNAME/WP_APP_PASSWORD)"}
    url, auth = base
    path = _resolve_wp_path(path)  # outside the network try: a profile error must not read as "unreachable"
    import json as _json
    import httpx
    headers = {}
    content = None
    if json_body is not None:
        content = _json.dumps(json_body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
        # Sign over "timestamp.body" and send the timestamp so WordPress can enforce freshness and block
        # replay. WordPress falls back to body-only verification if a timestamp is ever absent.
        import hmac as _hmac
        import time as _time
        secret = (_portal_env().get("MONGOOSE_WEBHOOK_SECRET") or "").encode()
        if secret:
            ts = str(int(_time.time()))
            sig = _hmac.new(secret, (ts + ".").encode() + content, hashlib.sha256).hexdigest()
            headers["X-Mongoose-Timestamp"] = ts
            headers["X-Mongoose-Signature"] = sig
        # Ed25519 detached signature over the body. The plugin verifies this with Mongoose's PUBLIC key, so
        # a compromised WordPress (which can read every option, including the old HMAC secret) still cannot
        # forge a write: the private key never leaves this machine.
        ed = _sign_body(content)
        if ed:
            headers["X-Mongoose-Ed25519"] = ed
    try:
        r = httpx.request(method, f"{url}{path}", auth=auth, headers=headers,
                          content=content, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": f"WordPress unreachable: {type(exc).__name__}"}
    if r.status_code == 404:
        return {"ok": False, "status": 404,
                "error": f"sync endpoint not found; deploy the {_profile.rtm_slug()} plugin first"}
    if r.status_code >= 400:
        # Surface the WordPress error BODY, not just the status. A bare "returned 400" hid the real
        # reason (e.g. "version is not an integer") and made a permanent validation failure look like a
        # transient blip; the message makes every sync error self-diagnosing.
        detail = ""
        try:
            j = r.json()
            detail = str(j.get("message") or j.get("error") or j.get("code") or "")
        except Exception:
            detail = (r.text or "").strip()[:200]
        return {"ok": False, "status": r.status_code,
                "error": f"WordPress returned {r.status_code}" + (f": {detail}" if detail else "")}
    try:
        return {"ok": True, "status": r.status_code, "data": r.json()}
    except Exception:
        # A 2xx we cannot parse is NOT a confirmed success (e.g. a PHP warning or HTML error page inside a
        # 200). Every plugin sync endpoint returns JSON on real success, so report this honestly , then a
        # send/ack/publish is retried instead of Mongoose telling M an announcement or receipt "went out".
        return {"ok": False, "status": r.status_code,
                "error": f"WordPress returned {r.status_code} but the body was not valid JSON"}



# ── Readiness ────────────────────────────────────────────────────────────────

def portal_readiness(*, probe_wp: bool = True) -> dict[str, Any]:
    """Full readiness report for the leaseholder portal: bank details, WordPress connection
    (live-probed), claim keypair, PyNaCl, demand PDFs, 18 known flats, private PDF directory."""
    env = _portal_env()
    checks: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    bank_ok = _bank_details_ok(env)
    checks.append({"label": "Bank details configured", "ok": bank_ok, "severity": "error",
                   "detail": "RTM_PORTAL_BANK_NAME/ACCOUNT_NAME/SORT_CODE/ACCOUNT_NUMBER must all be set"
                             if not bank_ok else "All four bank fields present"})
    if not bank_ok:
        errors.append("Bank details missing; portal cannot show payment information")

    wp_ok = _wp_configured(env)
    wp_detail = "WP_URL / WP_USERNAME / WP_APP_PASSWORD not set"
    if wp_ok and probe_wp:
        probe = _wp_call("GET", "/wp-json")
        wp_ok = bool(probe.get("ok"))
        wp_detail = "Live connection OK" if wp_ok else (probe.get("error") or "probe failed")
    elif wp_ok:
        wp_detail = "Credentials present (probe skipped)"
    checks.append({"label": "WordPress connection", "ok": wp_ok, "severity": "error", "detail": wp_detail})
    if not wp_ok:
        errors.append("WordPress connection not ready")

    snap_h = snapshot_publish_health()
    checks.append({"label": "Portal snapshot publishing", "ok": snap_h["ok"], "severity": "error",
                   "detail": ("Last publish OK" if snap_h["ok"] else
                              f"FAILING x{snap_h['consecutive_failures']}: {snap_h['last_error']} "
                              f"(last OK {snap_h['last_ok_at'] or 'never'}); WordPress is showing stale data")})
    if not snap_h["ok"]:
        errors.append(f"Portal snapshot publish failing: {snap_h['last_error']}")

    nacl_ok = _nacl() is not None
    checks.append({"label": "PyNaCl (claim decryption)", "ok": nacl_ok, "severity": "error",
                   "detail": "pip install pynacl" if not nacl_ok else "Available"})
    if not nacl_ok:
        errors.append("PyNaCl missing; encrypted claims cannot be processed")

    kp = _load_keypair()
    checks.append({"label": "Claim encryption keypair", "ok": bool(kp), "severity": "warning",
                   "detail": "Run: mongoose rtm portal generate-keypair" if not kp
                             else f"Found at {_keypair_path()}"})
    if not kp:
        warnings.append("Claim keypair not generated; claims will not be accepted until keypair exists")

    pdf_dir = env.get("PC_PORTAL_PRIVATE_DIR", "")
    pdf_dir_ok = bool(pdf_dir and Path(pdf_dir).expanduser().exists())
    checks.append({"label": "Private PDF storage directory", "ok": pdf_dir_ok, "severity": "warning",
                   "detail": "Set PC_PORTAL_PRIVATE_DIR to a directory outside the WordPress web root"
                             if not pdf_dir_ok else f"Directory exists: {pdf_dir}"})

    try:
        from .rtm_management import list_flats
        flats = list_flats()
        expected = 18
        checks.append({"label": f"All {expected} flats seeded", "ok": len(flats) >= expected,
                       "severity": "warning",
                       "detail": f"{len(flats)}/{expected} flats in database. Run: mongoose rtm portal seed-flats"})
    except Exception as exc:
        checks.append({"label": "Flats check", "ok": False, "severity": "warning",
                       "detail": f"Could not check flats: {exc}"})

    try:
        missing = missing_portal_assets().get("missing", [])
        checks.append({"label": "Demand PDFs attached", "ok": not missing, "severity": "warning",
                       "detail": ("All portal demands have PDFs" if not missing
                                  else f"{len(missing)} demand(s) missing a PDF")})
    except Exception:
        pass

    overall_ok = bank_ok and wp_ok and nacl_ok and not errors
    return {"ok": overall_ok, "ready": overall_ok and bool(kp), "version": PORTAL_VERSION,
            "status": "implemented", "checks": checks, "warnings": warnings, "errors": errors}


# ── Snapshot ─────────────────────────────────────────────────────────────────

# Human block addresses for the two Example Court buildings, so a leaseholder sees "Flat 1, 1 Example
# Road" instead of the internal id "1ECF1". Keyed by the block code stored on the flat. An unknown code
# falls back to the raw block, and a flat with no number falls back to the flat_id, so this never invents
# an address it does not have.
def _block_address() -> dict[str, str]:
    """Block code -> road, from the company config (data_dir real / packaged fictional), so no real
    address is a source literal."""
    from .rtm_example_court import company_profile
    return {b.upper(): (v.get("road") or "") for b, v in company_profile().get("blocks", {}).items()}


def _flat_label(flat: dict[str, Any]) -> str:
    """A resident-friendly label for a flat, e.g. "Flat 1, <block road>". Falls back to the block code,
    then the flat_id, so it is always something and never a fabricated address."""
    fn = flat.get("flat_number")
    num = "" if fn is None else str(fn).strip()  # flat_number 0 is valid (a test flat), so do not treat 0 as empty
    block = str(flat.get("block") or "").strip()
    addr = _block_address().get(block.upper(), block)
    if num and addr:
        return f"Flat {num}, {addr}"
    return str(flat.get("flat_id") or "")


def _safe_flat_row(flat: dict[str, Any], *, include_statement: bool = False) -> dict[str, Any]:
    """Strip director-only and excluded fields from a flat row for the portal snapshot."""
    from .rtm_management import agent_cc_for_editing as _agent_cc_for_editing
    row: dict[str, Any] = {
        "flat_id": flat.get("flat_id"),
        "block": flat.get("block"),
        "flat_number": flat.get("flat_number"),
        "flat_label": _flat_label(flat),
        "leaseholder_name": flat.get("leaseholder_name", ""),
        # The director's chosen greeting (e.g. a joint "Alex and Sam Toby", or a titled "Dr Smith"), so
        # the portal greets joint holders and titled leaseholders correctly instead of guessing a first name.
        "greeting_name": flat.get("greeting_name", ""),
        "claim_enabled": True,
    }
    # Mark a test/fixture flat so the WordPress director views can keep it out of counts and totals
    # (the "stays 18" rule) while still letting a director invite it. Only present when true.
    if int(flat.get("excluded") or 0):
        row["excluded"] = True
    # Leaseholder email (director-appropriate contact info): powers the Flats & Members email column and
    # lets a director who is also a leaseholder self-link their own flat by email match. Only ever shown
    # to the director surface; a leaseholder sees only their own flat.
    try:
        email = _flat_email_map().get(str(flat.get("flat_id") or "").upper(), "")
        if email:
            row["leaseholder_email"] = email
    except Exception:
        pass
    # The leaseholder's OWN alternative addresses, so a building-wide announcement copies their spare
    # inboxes too. This field holds the alts only; the managing agent lives in contact.demand_cc (below),
    # and the plugin's announce path now copies BOTH on notices, matching the owner's rule that a managing
    # agent is a party to ALL correspondence (see correspondence_cc), not just financial documents. Only
    # present when there is at least one, so an ordinary flat's row is unchanged.
    try:
        from .rtm_management import leaseholder_alt_emails
        alts = leaseholder_alt_emails(str(flat.get("flat_id") or ""))
        if alts:
            row["leaseholder_cc"] = alts
    except Exception:
        pass
    # Payment reference is included for EVERY flat (not gated on statement data): WordPress matches a
    # sign-up against it (reference + document number + on-file email) and provisions instantly. It is
    # the same director-appropriate exposure class as leaseholder_name/email already in this row, and it
    # is what the leaseholder's own "how to pay" card shows.
    row["payment_reference_display"] = flat.get("payment_reference", "") or ""
    # Director-appropriate CONTACT detail set for the Flats details editor. Same exposure class as
    # leaseholder_name/email already on this row; grouped under `contact` so a leaseholder-facing view can
    # simply ignore it. Editing these on WordPress queues a change Mongoose validates and applies.
    row["contact"] = {
        "leaseholder_name": flat.get("leaseholder_name", "") or "",
        "greeting_name": flat.get("greeting_name", "") or "",
        "leaseholder_email": row.get("leaseholder_email", "") or (flat.get("leaseholder_email", "") or ""),
        "leaseholder_email_alt1": flat.get("leaseholder_email_alt1", "") or "",
        "leaseholder_email_alt2": flat.get("leaseholder_email_alt2", "") or "",
        "demand_cc": _agent_cc_for_editing(flat),  # raw stored value for the editor; sending uses correspondence_cc()
        "payment_reference": flat.get("payment_reference", "") or "",
        "correspondence_address": flat.get("correspondence_address", "") or "",
        "correspondence_address_alt1": flat.get("correspondence_address_alt1", "") or "",
    }
    # Open demands per flat (director-appropriate exposure, same class as leaseholder_email already here):
    # lets the WordPress Flats & Members view offer a "Mark paid" button PER demand/period, keyed on the
    # document_number, instead of one flat-level button that silently clears the oldest open period. We
    # ALWAYS set the key (even to []) so the plugin can tell "paid up" (empty) from "old snapshot" (absent)
    # and only fall back to the flat-level button for a pre-1.0.27 snapshot. Outstanding is computed live
    # from allocations, so a genuinely-owed period appears regardless of any stale stored status.
    try:
        from .rtm_management import list_demands
        od = []
        for d in list_demands(flat=str(flat.get("flat_id") or "")):
            if d.get("status") in {"Paid", "Superseded", "Cancelled"}:
                continue
            if int(d.get("amount_pence") or 0) <= 0:      # a nil/test demand: nothing to pay
                continue
            if int(d.get("outstanding_pence") or 0) <= 0:  # already settled by allocations
                continue
            od.append({
                "document_number": d.get("document_number"),
                "period": d.get("period", ""),
                "amount": d.get("amount"),
                "outstanding": d.get("outstanding"),
                "outstanding_pence": int(d.get("outstanding_pence") or 0),
                "status": d.get("status", ""),
            })
        # The reserve vs service-charge sub-account split for this flat's half-year demand, so the WordPress
        # mark-paid confirmation can tell the director how to split the payment between the two sub bank
        # accounts. Computed once per flat (only when there IS an open demand) and attached to each entry.
        if od:
            try:
                from .rtm_trust import subaccount_split
                sp = subaccount_split(str(flat.get("flat_id") or ""))
                ss = sp.get("subaccount_split", {})
                for e in od:
                    e["reserve_split"] = ss.get("reserve_fund_trust")
                    e["service_split"] = ss.get("service_charge_trust")
                    e["reserve_pence"] = int(sp.get("reserve_pence") or 0)
                    e["service_pence"] = int(sp.get("service_pence") or 0)
                    e["split_total_pence"] = int(sp.get("total_pence") or 0)
            except Exception:
                pass
        row["open_demands"] = od
    except Exception:
        pass
    if include_statement:
        try:
            from .rtm_management import flat_statement
            st = flat_statement(str(flat.get("flat_id") or ""))
            row["balance_pence"] = st.get("balance_pence", 0)
            row["balance"] = st.get("balance", "\u00a30.00")
            row["position"] = st.get("position", "clear")
            # Sign-safe credit fields, so the portal shows "In credit / paid in advance £X" instead of a
            # bare negative balance. Only meaningful when position == 'credit'.
            row["in_credit"] = bool(st.get("in_credit"))
            row["credit_display"] = st.get("credit_display", "\u00a30.00")
            row["on_account"] = st.get("on_account", "\u00a30.00")
            row["on_account_pence"] = int(st.get("on_account_pence") or 0)
        except Exception:
            pass
        # Agreed payment plan (arrears instalments), so the portal can show "Payment plan agreed" + the
        # schedule and never re-offer "agree plan" for a flat that already has one.
        # plan_progress is payment_plan_for PLUS what has actually been paid against the schedule. The portal
        # needs the paid side to colour a plan debit amber rather than red (owed, but not in default), to draw
        # the paid segment of the collection bar, and to state a percentage that counts plan payments. A bare
        # schedule with no payment state can support none of that. It rides inside the flat row, so no
        # top-level allowlist change is needed, and none of its field names hit the blocked-field denylist.
        try:
            from .rtm_payment_plans import plan_progress
            row["payment_plan"] = plan_progress(str(flat.get("flat_id") or ""))
        except Exception:
            row["payment_plan"] = None
        # OVERDUE STATE. Without it the portal shows a leaseholder a month past due the identical card as
        # one whose demand was issued yesterday: "Action needed, there is X to pay", in amber, with no time
        # dimension and the word "overdue" appearing nowhere. Comes from the same overdue_stage() the
        # chaser letters use, so the screen cannot say something different from the letter in the post.
        try:
            from .rtm_overdue import overdue_stage, portal_text
            _st = overdue_stage(str(flat.get("flat_id") or ""))
            _txt = portal_text(str(flat.get("flat_id") or ""))
            row["overdue"] = ({"stage": _st.get("stage"), "days": _st.get("days_overdue"),
                               "outstanding": _st.get("outstanding"),
                               "due_date": _st.get("due_date"),
                               "tone": _txt.get("tone"), "heading": _txt.get("heading"),
                               "text": _txt.get("text")}
                              if _txt.get("show") else None)
        except Exception:
            row["overdue"] = None
        # Per-schedule apportionment breakdown + credit notes for this flat (spec R29), so the leaseholder
        # portal can show "Schedule 1: X%, \u00a3Y / Schedule 2: Z%, \u00a3W" and any credit notes. Non-sensitive.
        try:
            from . import rtm_apportionment as APP
            from .rtm_management import _money
            fid = str(flat.get("flat_id") or "")
            appn = APP.flat_apportionment(fid)  # schedule -> ppm
            sched_names = {s["code"]: s["name"] for s in APP.list_schedules()}
            # This flat's per-schedule half-year charge is COMPUTED from the apportionment (deterministic,
            # penny-exact), not read from demand rows, so the split is correct whether the demand is one
            # combined document or not.
            from ..db import connect
            with connect() as con:
                yr = con.execute("SELECT MAX(year) FROM rtm_mgmt_budget_lines").fetchone()
            year = str(yr[0]) if yr and yr[0] else ""
            breakdown = []
            for code, ppm in sorted(appn.items()):
                charge = 0
                if year:
                    r = APP.apportion_schedule_period(code, year)
                    if r.get("ok"):
                        charge = int(r["shares"].get(fid, 0))
                breakdown.append({"schedule": code, "name": sched_names.get(code, code),
                                  "permille": ppm, "percent": APP.ppm_to_pct(ppm),
                                  "period_charge_pence": charge, "period_charge": _money(charge)})
            if breakdown:
                row["schedule_apportionment"] = breakdown
            cns = APP.credit_notes_for_flat(fid)
            if cns:
                row["credit_notes"] = [{"schedule": c["schedule"], "amount": _money(int(c["amount_pence"])),
                                        "issued_date": c["issued_date"], "credit_note_id": c["id"]} for c in cns]
        except Exception:
            pass
    return row


def _demand_rows_for_snapshot() -> list[dict[str, Any]]:
    """Portal-safe demand metadata with pdf_key + sha256 (NEVER pdf_storage_path).

    create_demand deliberately does not gate on flat completeness (that broke the internal
    RTM test suite's established use of it as a lightweight fixture; see rtm_management.py).
    The actual safety property, a leaseholder must never SEE a demand raised against an
    unnamed placeholder flat, is enforced HERE instead: any demand whose flat has no
    leaseholder_name is excluded from what gets published (external audit B1)."""
    _ensure_portal_tables()
    from ..db import connect
    try:
        from .rtm_management import list_demands, list_flats
        raw = list_demands()
        demands = raw.get("demands", []) if isinstance(raw, dict) else (raw or [])
        # include_excluded so a test flat's own (zero) demand still publishes for the sign-up match.
        flats = list_flats(include_excluded=True) or []
        named_flats = {f.get("flat_id") for f in flats
                       if str(f.get("leaseholder_name") or "").strip()}
    except Exception:
        return []
    with connect() as con:
        pdfs = {r["document_number"]: dict(r) for r in
                con.execute("SELECT document_number, pdf_key, pdf_sha256 FROM portal_demand_pdfs").fetchall()}
    out = []
    _split_cache: dict[str, tuple[int, int]] = {}   # per-flat reserve/service pence (the split is per flat/half-year)

    def _split_for(flat_id: str) -> tuple[int, int]:
        fid = str(flat_id or "")
        if fid not in _split_cache:
            try:
                from .rtm_trust import subaccount_split
                sp = subaccount_split(fid)
                _split_cache[fid] = (int(sp.get("reserve_pence") or 0), int(sp.get("service_pence") or 0))
            except Exception:
                _split_cache[fid] = (0, 0)
        return _split_cache[fid]

    for d in demands or []:
        if d.get("flat_id") not in named_flats:
            continue
        doc = d.get("document_number")
        pdf = pdfs.get(doc, {})
        # The reserve/service sub-account split of this demand, so the portal can show it for ANY demand
        # (including already-paid ones) without recomputing WordPress-side. Engine is the single source.
        reserve_pence, service_pence = _split_for(d.get("flat_id"))
        out.append({
            "document_number": doc,
            "flat_id": d.get("flat_id"),
            "period": d.get("period"),
            "amount": d.get("amount"),
            "due_date": d.get("due_date"),
            "status": d.get("status"),
            "payment_reference": d.get("payment_reference"),
            "reserve_pence": reserve_pence,
            "service_pence": service_pence,
            "pdf_key": pdf.get("pdf_key", ""),
            "pdf_sha256": pdf.get("pdf_sha256", ""),
        })
    return out


def _documents_build_id() -> str:
    """Id of the letter recipe currently published. Empty on failure, which makes the portal hide the
    download buttons rather than risk offering a document it cannot vouch for."""
    try:
        from .rtm_overdue import _document_build_id
        return _document_build_id()
    except Exception:
        return ""


def _collection_for_snapshot() -> dict[str, Any]:
    """Plan-aware collection bands for the portal chart. Never fatal: on any failure the portal keeps its
    previous chart rather than publishing a snapshot the strict validator would reject wholesale."""
    try:
        from .rtm_management import collection_position
        return collection_position()
    except Exception:
        return {}


def _periods_for_snapshot() -> dict[str, Any]:
    """Charging periods (newest first) + which is current, so WordPress offers the SAME period selector
    as the Cockpit and can split "this period" from "brought forward" instead of blending newly-issued
    charges into arrears. Best-effort: publishing a snapshot must never fail over a selector."""
    try:
        from .rtm_management import list_periods
        return list_periods()
    except Exception:
        return {"ok": False, "periods": [], "current": "", "count": 0}


def _receipt_rows_for_snapshot() -> list[dict[str, Any]]:
    """Payment-receipt rows for the snapshot, one per stored receipt PDF, only for NAMED flats (same
    safety boundary as demands). Each carries its pdf_key so the plugin can offer a flat-gated download."""
    _ensure_portal_tables()
    from ..db import connect
    try:
        from .rtm_management import list_flats
        named = {str(f.get("flat_id") or "").upper() for f in (list_flats(include_excluded=True) or [])
                 if str(f.get("leaseholder_name") or "").strip()}
    except Exception:
        return []
    out = []
    with connect() as con:
        rows = con.execute(
            "SELECT receipt_no, flat_id, pdf_key, period, amount, received_date, document_number "
            "FROM portal_receipt_pdfs ORDER BY received_date DESC, receipt_no").fetchall()
    for r in rows:
        d = dict(r)
        if str(d.get("flat_id") or "").upper() not in named:
            continue
        out.append({"receipt_no": d["receipt_no"], "flat_id": d["flat_id"], "period": d.get("period", ""),
                    "amount": d.get("amount", ""), "received_date": d.get("received_date", ""),
                    "pdf_key": d.get("pdf_key", ""),
                    # The demand this receipt pays: lets WordPress attach it to the exact demand row
                    # rather than guessing by flat (which breaks the moment a flat has two periods).
                    "document_number": d.get("document_number", "")})
    return out


def build_portal_snapshot(*, full: bool = False) -> dict[str, Any]:
    """Build a minimised portal-safe snapshot from RTM management data.

    full=False: safe admin metadata per flat only. full=True: statement data for every flat
    (the WordPress plugin only reveals a flat's statement to its claimed+active account).
    NEVER includes FTT strategy, evidence, supplier payables, director notes, claim hashes
    or any other excluded field; the exclusion checker runs on every build."""
    try:
        from .rtm_management import list_flats
        # Include excluded (test/fixture) flats so their sign-up + portal can be exercised, but count only
        # the real charging flats for flat_count, so "total flats" stays 18.
        flats = list_flats(include_excluded=True)
    except Exception as exc:
        return {"ok": False, "error": f"Could not load flats: {exc}"}

    # ALWAYS include the per-flat statement (balance + position): the WordPress Flats table renders those
    # columns unconditionally, so without them every publish (full=False) left them blank. It is a cheap
    # local computation (one statement per flat), so there is no reason to gate it on the full flag.
    flat_snapshots = [_safe_flat_row(f, include_statement=True) for f in flats]
    real_flat_count = sum(1 for f in flats if not int(f.get("excluded") or 0))

    bank_env = _portal_env()
    _bd = _resolve_bank_details(bank_env)
    bank_details = ({**_bd, "payment_instruction": PAYMENT_INSTRUCTION} if _bd else None)

    snapshot = {
        "ok": True,
        "version": PORTAL_SNAPSHOT_SCHEMA_VERSION,
        "status": "implemented",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "full": bool(full),
        "flat_count": real_flat_count,
        "flats": flat_snapshots,
        "demands": _demand_rows_for_snapshot(),
        "receipts": _receipt_rows_for_snapshot(),
        "periods": _periods_for_snapshot(),
        # Plan-aware collection bands + both percentages, so the portal chart can separate money DEFERRED by
        # an agreed plan from money in genuine default, and can state a collection figure that counts plan
        # instalments already paid. Aggregates only, already derivable from `demands` and each flat's
        # payment_plan, so nothing new is exposed.
        "collection": _collection_for_snapshot(),
        # The recipe the published documents were built from. WordPress offers a download ONLY when the
        # stored document carries this exact id, so a letter can never be served after its wording has
        # changed: the old key stops matching and the button disappears instead of handing over a
        # document that opens, looks correct, and is out of date.
        "documents_build_id": _documents_build_id(),
        "bank_details": bank_details,
        "bank_details_ok": bank_details is not None,
        "caveat_7day": CAVEAT_7DAY,
    }
    # Open member votes ride out so the WordPress member portal can show them (member-only there).
    # Tallies only, no flat-level cast detail; membership eligibility stays enforced in Mongoose.
    try:
        from .rtm_votes import votes_snapshot
        snapshot["votes"] = votes_snapshot()
    except Exception:
        snapshot["votes"] = []
    # The RTM-member flat list so the portal can show the votes tab to members only and reject
    # ineligible casts up front. Just flat identifiers (not sensitive); Mongoose re-checks on ingest.
    try:
        from .rtm_members import member_flat_ids
        snapshot["member_flat_ids"] = sorted(member_flat_ids())
    except Exception:
        snapshot["member_flat_ids"] = []
    _assert_no_excluded_fields(snapshot)
    return snapshot


def _assert_no_excluded_fields(obj: Any, path: str = "") -> None:
    """Recursively check that no excluded field appears in the snapshot.
    Raises AssertionError on violation. Spec section 8 regression gate."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_lower = k.lower().replace("-", "_")
            if k_lower in _SNAPSHOT_EXCLUSIONS:
                raise AssertionError(
                    f"SNAPSHOT EXCLUSION VIOLATION: field '{k}' at path '{path}' "
                    f"must never appear in the portal snapshot. Spec section 8."
                )
            _assert_no_excluded_fields(v, path=f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _assert_no_excluded_fields(item, path=f"{path}[{i}]")


# ── Publish to WordPress ──────────────────────────────────────────────────────

def publish_portal_snapshot(*, full: bool = False) -> dict[str, Any]:
    """Build and push the portal snapshot to WordPress via the protected sync endpoint."""
    snap = build_portal_snapshot(full=full)
    if not snap.get("ok"):
        return {"ok": False, "error": snap.get("error")}
    res = _wp_call("POST", _profile.wp_route("/sync/snapshot"), json_body=snap, timeout=40.0)
    # VERSION SKEW SELF-HEAL. WordPress enforces a strict top-level ALLOWLIST, so a snapshot key added
    # by a newer Mongoose (e.g. "periods") is rejected 400 by an older plugin , and because publishing
    # is all-or-nothing that wedges the ENTIRE mirror: every later payment, receipt and balance silently
    # stops reaching the portal while Mongoose keeps reporting the ledger as correct. A cosmetic new
    # section must never be able to block financial truth, so drop the key it named and republish. The
    # dropped key is recorded, and the plugin update restores the richer snapshot.
    dropped: list[str] = []
    for _ in range(4):                       # bounded: one pass per unknown key, never a loop
        err = str(res.get("error") or "")
        if res.get("ok") or "unrecognised top-level section" not in err:
            break
        key = err.rsplit(":", 1)[-1].strip()
        if not key or key not in snap:
            break
        snap.pop(key, None)
        dropped.append(key)
        res = _wp_call("POST", _profile.wp_route("/sync/snapshot"), json_body=snap, timeout=40.0)
    ok = bool(res.get("ok"))
    detail = res.get("error") or f"flats={snap['flat_count']}"
    if dropped:
        detail += f" (dropped unsupported key(s) for this plugin version: {', '.join(dropped)})"
    _record_sync_run("publish_snapshot", ok, detail)
    if not ok:
        return {"ok": False, "error": res.get("error"), "flat_count": snap["flat_count"]}
    # Couple the financials to the snapshot: publishing the snapshot ALONE (without the director summary)
    # is exactly what left the pie chart blank. Always refresh the director financials alongside, so the
    # two mirrors can never drift no matter which entry point a caller uses. Best-effort; never fails the
    # snapshot publish, and the source + plugin guards refuse any zero-demanded payload.
    director = None
    try:
        director = publish_director_summary()
    except Exception as _exc:
        director = {"ok": False, "error": str(_exc)}
    try:
        publish_reconciliation()
    except Exception:
        pass
    # Documents: push the metadata index + any NEW file bytes so the plugin's Documents page mirrors the
    # Cockpit doc store, and the Bank-reconciliation evidence links resolve. Best-effort; never fails the
    # snapshot publish. The file push is idempotent by content hash, so it is a cheap no-op after the first.
    try:
        publish_documents_index()
        publish_document_files()
    except Exception:
        pass
    # Remember WHAT we published (content hash, volatile fields excluded) so the guard can tell a real
    # data change from noise and only republish when the meaning actually changed.
    try:
        _store_published_hash(_snapshot_content_hash(snap))
    except Exception:
        pass
    return {"ok": True, "flat_count": snap["flat_count"],
            "pushed_at": datetime.now(timezone.utc).isoformat(),
            "wp_status": res.get("status"),
            "director_summary": (director or {}).get("ok")}


def _published_hash_path() -> Path:
    data_dir = (os.environ.get("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    return Path(data_dir) / "portal" / "last_published.sha256"


def _snapshot_content_hash(snap: dict[str, Any]) -> str:
    """Stable hash of the MEANINGFUL snapshot content: volatile fields (generated_at) removed and keys
    sorted, so an unchanged ledger hashes identically build-to-build (no needless republishing)."""
    import hashlib as _h
    import json as _json
    volatile = {"generated_at", "pushed_at", "full"}  # build flags, not meaningful ledger content
    body = {k: v for k, v in snap.items() if k not in volatile}
    return _h.sha256(_json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _store_published_hash(h: str) -> None:
    p = _published_hash_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(h)


def _last_published_hash() -> str:
    try:
        return _published_hash_path().read_text().strip()
    except Exception:
        return ""


def snapshot_guard() -> dict[str, Any]:
    """The intelligent self-healing publish: keep WordPress equal to Mongoose (the ledger of record) with
    no blind timer re-pushes. Each run does two things, in priority order:

      1) TAMPER FIRST. Ask WordPress what it is serving; if it is newer than our last publish, something
         other than Mongoose wrote it. check_snapshot_drift() engages the durable security hold; we alarm
         and STOP (never auto-overwrite a possible attacker without a human clearing the hold).

      2) SELF-HEAL. Otherwise, hash the snapshot Mongoose would publish now and compare it to what we last
         published. Republish ONLY if the meaningful content changed (a mark-paid from chat, a new demand,
         an apportionment edit, a vote). If nothing changed, do nothing: no wasted push.

    This closes the gap that let the 07-18 tamper sit for five days: drift is caught within one interval,
    and any Mongoose-side change reaches the portal on its own."""
    if (os.getenv("MONGOOSE_PORTAL_SYNC", "on") or "").strip().lower() == "off" or _security_hold_path().exists():
        return {"ok": True, "skipped": "paused_or_held"}
    if not _wp_configured():
        return {"ok": True, "skipped": "wp_not_configured"}
    drift = check_snapshot_drift()
    if not drift.get("ok"):
        return {"ok": False, "error": drift.get("error"), "phase": "drift_check"}
    if drift.get("drift"):
        return {"ok": True, "action": "held_tamper", "alarm": True, "reason": drift.get("reason")}
    # No tamper: republish only if our content moved on.
    snap = build_portal_snapshot(full=False)
    if not snap.get("ok"):
        return {"ok": False, "error": snap.get("error"), "phase": "build"}
    current = _snapshot_content_hash(snap)
    if current == _last_published_hash():
        return {"ok": True, "action": "in_sync"}
    pub = publish_portal_snapshot(full=False)
    return {"ok": bool(pub.get("ok")), "action": "republished" if pub.get("ok") else "republish_failed",
            "flat_count": pub.get("flat_count"), "error": pub.get("error")}


def publish_demand_pdfs(*, limit: int = 200) -> dict[str, Any]:
    """Push each demand PDF to WordPress so the leaseholder can download it from their portal.

    Safety boundary matches the snapshot: a PDF is published ONLY for a demand whose flat has a
    leaseholder_name (a real, named flat), never a placeholder. The plugin stores the bytes keyed by
    pdf_key and its /portal/demand-pdf/{key} route only serves it to that flat's own logged-in account.
    pdf_storage_path stays server-side; only the PDF bytes (base64) plus safe metadata are sent."""
    _ensure_portal_tables()
    import base64 as _b64
    from ..db import connect
    with connect() as con:
        rows = con.execute(
            """
            SELECT p.document_number, p.pdf_key, p.pdf_sha256, p.pdf_storage_path, d.flat_id
            FROM portal_demand_pdfs p
            JOIN rtm_mgmt_demands d ON d.document_number = p.document_number
            JOIN rtm_mgmt_flats  f ON f.flat_id = d.flat_id
            WHERE TRIM(COALESCE(f.leaseholder_name, '')) <> ''
            ORDER BY p.document_number
            LIMIT ?
            """, (max(1, min(limit, 1000)),)).fetchall()
    pushed, skipped, errors = 0, 0, []
    for r in rows:
        row = dict(r)
        path = Path(str(row.get("pdf_storage_path") or "")).expanduser()
        if not path.exists():
            skipped += 1
            continue
        try:
            raw = path.read_bytes()
        except Exception as exc:
            errors.append(f"{row.get('document_number')}: read failed ({type(exc).__name__})")
            continue
        body = {
            "pdf_key": row.get("pdf_key"),
            "document_number": row.get("document_number"),
            "flat_id": row.get("flat_id"),
            "content_b64": _b64.b64encode(raw).decode("ascii"),
            "sha256": row.get("pdf_sha256") or hashlib.sha256(raw).hexdigest(),
        }
        res = _wp_call("POST", _profile.wp_route("/sync/demand-pdf"), json_body=body, timeout=40.0)
        if res.get("ok"):
            pushed += 1
        else:
            errors.append(f"{row.get('document_number')}: {res.get('error')}")
    ok = not errors
    _record_sync_run("publish_demand_pdfs", ok, f"pushed={pushed} skipped_missing={skipped} errors={len(errors)}")
    return {"ok": ok, "pushed": pushed, "skipped_missing_file": skipped,
            "candidates": len(rows), "errors": errors}


def publish_receipt_pdfs(*, limit: int = 200) -> dict[str, Any]:
    """Push each payment-receipt PDF to WordPress so the leaseholder can download it, mirroring
    publish_demand_pdfs. Reuses the plugin's generic flat-gated PDF store + /sync/demand-pdf route
    (keyed by pdf_key with flat_id), so the SAME /portal/demand-pdf/{key} route serves it only to that
    flat's logged-in account. Only named flats. pdf_storage_path stays server-side."""
    _ensure_portal_tables()
    import base64 as _b64
    from ..db import connect
    with connect() as con:
        rows = con.execute(
            """
            SELECT p.receipt_no, p.flat_id, p.pdf_key, p.pdf_sha256, p.pdf_storage_path
            FROM portal_receipt_pdfs p
            JOIN rtm_mgmt_flats f ON f.flat_id = p.flat_id
            WHERE TRIM(COALESCE(f.leaseholder_name, '')) <> ''
            ORDER BY p.receipt_no LIMIT ?
            """, (max(1, min(limit, 1000)),)).fetchall()
    pushed, skipped, errors = 0, 0, []
    for r in rows:
        row = dict(r)
        path = Path(str(row.get("pdf_storage_path") or "")).expanduser()
        if not path.exists():
            skipped += 1
            continue
        try:
            raw = path.read_bytes()
        except Exception as exc:
            errors.append(f"{row.get('receipt_no')}: read failed ({type(exc).__name__})")
            continue
        body = {
            "pdf_key": row.get("pdf_key"),
            "document_number": row.get("receipt_no"),
            "flat_id": row.get("flat_id"),
            "content_b64": _b64.b64encode(raw).decode("ascii"),
            "sha256": row.get("pdf_sha256") or hashlib.sha256(raw).hexdigest(),
        }
        res = _wp_call("POST", _profile.wp_route("/sync/demand-pdf"), json_body=body, timeout=40.0)
        if res.get("ok"):
            pushed += 1
        else:
            errors.append(f"{row.get('receipt_no')}: {res.get('error')}")
    ok = not errors
    _record_sync_run("publish_receipt_pdfs", ok, f"pushed={pushed} skipped_missing={skipped} errors={len(errors)}")
    return {"ok": ok, "pushed": pushed, "skipped_missing_file": skipped,
            "candidates": len(rows), "errors": errors}


def send_portal_announcement(subject: str, body: str) -> dict[str, Any]:
    """Ask WordPress to email every on-file leaseholder a building announcement AND record it as a
    historical announcement in the portal. WordPress is the only side that can send from the RTM mailbox
    (Mongoose's email connectors are read-only), so the send happens there; this just triggers it, signed."""
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        return {"ok": False, "error": "subject and body are required"}
    res = _wp_call("POST", _profile.wp_route("/portal/announce"),
                   json_body={"subject": subject, "body": body}, timeout=60.0)
    _record_sync_run("send_announcement", bool(res.get("ok")),
                     res.get("error") or f"sent={res.get('sent')} of {res.get('recipients')}")
    return res


def send_receipt_email(flat_id: str, receipt_no: str = "") -> dict[str, Any]:
    """Send a leaseholder their payment receipt with a branded cover note. The receipt PDF is already in
    the WordPress store (pushed by publish_receipt_pdfs), so WordPress attaches and sends it from
    the RTM mailbox (profile site.rtm_email); Mongoose only triggers it and supplies the greeting + amount + date."""
    _ensure_portal_tables()
    from ..db import connect
    flat_id = (flat_id or "").strip().upper()
    with connect() as con:
        if receipt_no:
            r = con.execute("SELECT receipt_no, pdf_key, amount, received_date FROM portal_receipt_pdfs "
                            "WHERE flat_id=? AND receipt_no=?", (flat_id, receipt_no)).fetchone()
        else:
            # No receipt named: only safe while the flat has exactly ONE receipt. Once a second period
            # exists, silently emailing "the latest" could send a leaseholder the wrong period's
            # receipt, so refuse and make the caller name it rather than guess at a financial document.
            rows = con.execute("SELECT receipt_no, pdf_key, amount, received_date FROM portal_receipt_pdfs "
                               "WHERE flat_id=? ORDER BY received_date DESC, receipt_no", (flat_id,)).fetchall()
            if len(rows) > 1:
                return {"ok": False, "error": (
                    f"{flat_id} has {len(rows)} receipts on file; name which to send (receipt_no) "
                    "instead of guessing: " + ", ".join(str(dict(x)["receipt_no"]) for x in rows))}
            r = rows[0] if rows else None
    if not r:
        return {"ok": False, "error": f"no receipt on file for {flat_id}, attach one first"}
    rr = dict(r)
    try:
        from .rtm_management import _get_flat
        fl = _get_flat(flat_id) or {}
    except Exception:
        fl = {}
    greeting = str(fl.get("greeting_name") or "").strip()
    if not greeting:
        name = str(fl.get("leaseholder_name") or "")
        greeting = name.split()[0] if name else ""
    # CC the same parties as every other piece of correspondence. This path used to copy nobody, so a
    # leaseholder whose managing agent was copied on the DEMAND saw the agent dropped from the RECEIPT.
    try:
        from .rtm_management import correspondence_cc
        cc = correspondence_cc(flat_id)
    except Exception:
        cc = []
    body = {"flat_id": flat_id, "pdf_key": rr.get("pdf_key"), "receipt_no": rr.get("receipt_no"),
            "amount": rr.get("amount", ""), "received_date": rr.get("received_date", ""), "greeting": greeting,
            "cc": ", ".join(cc)}
    res = _wp_call("POST", _profile.wp_route("/portal/send-receipt"), json_body=body, timeout=60.0)
    _record_sync_run("send_receipt", bool(res.get("ok")), res.get("error") or f"{flat_id} -> {res.get('email', '')}")
    return res


def invite_flat_to_portal(flat_id: str, email: str = "") -> dict[str, Any]:
    """Invite a flat's leaseholder to the WordPress leaseholder portal: provision their account (on the
    on-file email unless one is passed) and send a branded invitation. Idempotent WordPress-side
    (run_once), so a re-invite of the same flat does not send a second email. Returns the invite result
    merged with a clear ok."""
    flat_id = (flat_id or "").strip().upper()
    if not flat_id:
        return {"ok": False, "error": "flat_id required"}
    body: dict[str, Any] = {"flat_id": flat_id}
    if (email or "").strip():
        body["email"] = email.strip()
    res = _wp_call("POST", _profile.wp_route("/portal/invite"), json_body=body, timeout=60.0)
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    ok = bool(res.get("ok")) and bool(data.get("ok", res.get("ok")))
    _record_sync_run("invite_flat", ok, res.get("error") or f"{flat_id} -> {data.get('email', '')} "
                     f"(notification={data.get('notification', '?')})")
    return {"ok": ok, "flat_id": flat_id, "email": data.get("email", ""),
            "new_account": data.get("new_account"), "email_sent": data.get("email_sent"),
            "notification": data.get("notification"), "error": res.get("error")}


# ── Director financials mirror + member votes ─────────────────────────────────

def publish_director_summary(*, year: str | None = None) -> dict[str, Any]:
    """Push the director financials summary to WordPress so the director admin can mirror the
    charts and itemised budget read-only. WordPress stores it verbatim; Mongoose stays the ledger."""
    from .rtm_director import director_summary
    summary = director_summary(year=year)
    if not summary.get("ok"):
        return {"ok": False, "error": summary.get("error", "summary failed")}
    # NEVER push a zero-demanded summary that would blank the portal pie chart. If the ledger genuinely has
    # demands but this computes £0 demanded, it is a miscompute (wrong year, mid-write), not reality ,
    # refuse to send it. The plugin also refuses to store a zeroing payload; this is the source-side guard.
    demanded_pence = int((summary.get("paid_vs_demanded") or {}).get("demanded_pence", 0) or 0)
    if demanded_pence <= 0:
        real = 0
        try:
            from ..db import connect
            with connect() as _c:
                real = int(_c.execute("SELECT COALESCE(SUM(amount_pence),0) FROM rtm_mgmt_demands "
                                      "WHERE status NOT IN ('Superseded','Cancelled')").fetchone()[0] or 0)
        except Exception:
            pass
        if real > 0:
            return {"ok": False, "error": f"refusing to publish a zero-demanded summary while the ledger holds "
                    f"£{real/100:,.2f} of demands (would blank the chart)"}
    res = _wp_call("POST", _profile.wp_route("/sync/director-summary"), json_body=summary, timeout=40.0)
    ok = bool(res.get("ok"))
    _record_sync_run("publish_director_summary", ok,
                     res.get("error") or f"pct_paid={summary['paid_vs_demanded']['pct_paid']}")
    if not ok:
        return {"ok": False, "error": res.get("error")}
    return {"ok": True, "pushed_at": datetime.now(timezone.utc).isoformat(), "wp_status": res.get("status")}


def publish_reconciliation(*, year: str | None = None) -> dict[str, Any]:
    """Push the bank-reconciliation view (recorded credits, expenditure with category suggestions,
    spend-vs-budget with warnings, book-vs-bank) to WordPress for the read-only Bank reconciliation page.
    Aggregates/derived rows only (no raw statement JSON). Its own endpoint, so it sidesteps the snapshot
    top-key allowlist, exactly like the director summary."""
    from .rtm_management import reconciliation_view
    view = reconciliation_view(year=year)
    # Give each evidence entry the stable pdf_key so the plugin links straight to the admin-gated file serve
    # (same key publish_document_files pushes the bytes under). doc_id/doc_type/filename come from the view.
    for _row in (view.get("credits") or []) + (view.get("expenditure") or []):
        for _ev in (_row.get("evidence") or []):
            try:
                _ev["pdf_key"] = _portal_document_key(int(_ev["doc_id"]))
            except Exception:
                _ev["pdf_key"] = ""
    res = _wp_call("POST", _profile.wp_route("/sync/reconciliation"), json_body=view, timeout=40.0)
    ok = bool(res.get("ok"))
    _record_sync_run("publish_reconciliation", ok,
                     res.get("error") or f"credits={view['counts']['credits']} exp={view['counts']['expenditure']}")
    return {"ok": ok, "error": res.get("error")}


def _ensure_portal_document_files_table(con) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS portal_document_files ("
                "doc_id INTEGER PRIMARY KEY, pdf_key TEXT UNIQUE, pushed_sha256 TEXT NOT NULL DEFAULT '', "
                "pushed_at TEXT NOT NULL DEFAULT '')")


def _portal_document_key(doc_id: int) -> str:
    """Stable, unguessable key a document is served under in WordPress. Minted once per document and kept,
    so a re-publish reuses it and the plugin's serve URL stays constant. Mirrors the demand-pdf pdf_key."""
    from ..db import connect
    with connect() as con:
        _ensure_portal_document_files_table(con)
        row = con.execute("SELECT pdf_key FROM portal_document_files WHERE doc_id=?", (int(doc_id),)).fetchone()
        if row:
            return row[0]
        key = secrets.token_urlsafe(32)
        con.execute("INSERT INTO portal_document_files(doc_id, pdf_key) VALUES(?,?)", (int(doc_id), key))
        con.commit()
        return key


def publish_documents_index(*, limit: int = 500) -> dict[str, Any]:
    """Push RTM document METADATA (never bytes, extracted_text or stored_path) to WordPress so the plugin
    Documents page can echo the Cockpit doc list. File bytes go separately via publish_document_files; the
    two share the stable pdf_key (the plugin serves the file admin-gated at /portal/document/{key})."""
    from .rtm_docs import list_documents
    docs = [d for d in list_documents(limit=max(1, min(limit, 2000)))
            if (d.get("doc_type") or "") != "export"]
    index = [{
        "id": d["id"], "doc_type": d.get("doc_type") or "", "filename": d.get("filename") or "",
        "description": d.get("description") or "",
        # The plugin's "Flat" column is where the owner has always read his own label for a document, so it
        # carries the real flat, or the description when the document is not for a flat.
        "flat_id": d.get("flat_id") or d.get("description") or "", "created_at": d.get("created_at") or "",
        "size_bytes": int(d.get("size_bytes") or 0), "mime": d.get("mime") or "",
        "summary": (d.get("summary") or "")[:600], "labels": d.get("labels") or [],
        "links": int(d.get("links") or 0), "total": (d.get("key_facts") or {}).get("total") or "",
        "total_confidence": (d.get("key_facts") or {}).get("total_confidence") or "",
        "total_basis": (d.get("key_facts") or {}).get("total_basis") or "",
        "has_thumb": bool(d.get("has_thumb")), "pdf_key": _portal_document_key(d["id"]),
    } for d in docs]
    body = {"ok": True, "documents": index, "count": len(index),
            "generated_at": datetime.now(timezone.utc).isoformat()}
    res = _wp_call("POST", _profile.wp_route("/sync/documents-index"), json_body=body, timeout=40.0)
    ok = bool(res.get("ok"))
    _record_sync_run("publish_documents_index", ok, res.get("error") or f"docs={len(index)}")
    return {"ok": ok, "count": len(index), "error": res.get("error")}


def publish_document_files(*, limit: int = 500) -> dict[str, Any]:
    """Push each RTM document's bytes to WordPress (served admin-gated, NOT flat-gated: invoices/contracts
    are internal). Idempotent by content hash: a document whose bytes already landed is skipped, so large
    policy PDFs are not re-sent every cycle. stored_path/extracted_text never leave the Mac."""
    import base64 as _b64
    from ..db import connect
    with connect() as con:
        _ensure_portal_document_files_table(con)
        docs = con.execute("SELECT id, stored_path, content_hash, filename, mime, doc_type FROM rtm_documents "
                           "WHERE COALESCE(doc_type,'') != 'export' "
                           "ORDER BY id DESC LIMIT ?", (max(1, min(limit, 2000)),)).fetchall()
        already = {r[0]: r[1] for r in con.execute(
            "SELECT doc_id, pushed_sha256 FROM portal_document_files").fetchall()}
    pushed, skipped, errors = 0, 0, []
    for doc_id, stored_path, content_hash, filename, mime, doc_type in docs:
        path = Path(str(stored_path or "")).expanduser()
        if not path.exists():
            skipped += 1
            continue
        try:
            raw = path.read_bytes()
        except Exception as exc:
            errors.append(f"doc {doc_id}: read failed ({type(exc).__name__})")
            continue
        sha = content_hash or hashlib.sha256(raw).hexdigest()
        if already.get(doc_id) == sha:
            skipped += 1
            continue
        key = _portal_document_key(doc_id)
        body = {"pdf_key": key, "doc_id": doc_id, "filename": filename or "document",
                "mime": mime or "application/octet-stream", "doc_type": doc_type or "",
                "content_b64": _b64.b64encode(raw).decode("ascii"), "sha256": sha}
        res = _wp_call("POST", _profile.wp_route("/sync/document-file"), json_body=body, timeout=60.0)
        if res.get("ok"):
            with connect() as con:
                con.execute("UPDATE portal_document_files SET pushed_sha256=?, pushed_at=? WHERE doc_id=?",
                            (sha, datetime.now(timezone.utc).isoformat(), doc_id))
                con.commit()
            pushed += 1
        else:
            errors.append(f"doc {doc_id}: {res.get('error')}")
    ok = not errors
    _record_sync_run("publish_document_files", ok, f"pushed={pushed} skipped={skipped} errors={len(errors)}")
    return {"ok": ok, "pushed": pushed, "skipped": skipped, "candidates": len(docs), "errors": errors}


def pull_flat_accounts() -> dict[str, Any]:
    """Pull per-flat linked WordPress accounts + last-login from WordPress and cache them, so the
    Cockpit Flats & Members view shows who has actually onboarded and when they last signed in.

    WordPress captures logins (wp_login hook) and serves them at /sync/flat-accounts; Mongoose pulls
    (CGNAT means WP cannot push). The cache is the display source for the last_login column."""
    from .rtm_members import cache_flat_accounts
    res = _wp_call("GET", _profile.wp_route("/sync/flat-accounts"))
    if not res.get("ok"):
        _record_sync_run("pull_flat_accounts", False, res.get("error") or "pull failed")
        return {"ok": False, "error": res.get("error"), "flats": 0}
    data = res.get("data") or {}
    accounts = data.get("accounts") or {}
    if not isinstance(accounts, dict):
        accounts = {}
    n = cache_flat_accounts(accounts)
    _record_sync_run("pull_flat_accounts", True, f"flats={n}")
    return {"ok": True, "flats": n}


def pull_member_changes() -> dict[str, Any]:
    """Pull director-requested RTM-membership changes from WordPress, apply them to the Mongoose ledger
    (the source of truth), and acknowledge them. The confirmed member_flat_ids ride back out in the next
    snapshot, so the WordPress toggle round-trips without WP becoming a second source of truth."""
    from .rtm_members import set_member
    res = _wp_call("GET", _profile.wp_route("/sync/member-changes"))
    if not res.get("ok"):
        _record_sync_run("pull_member_changes", False, res.get("error") or "pull failed")
        return {"ok": False, "error": res.get("error"), "applied": 0}
    data = res.get("data") or {}
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        changes = []
    if changes and not _portal_writes_trusted():
        # Membership drives voting rights; a compromised WordPress must not silently flip it.
        _record_sync_run("pull_member_changes", True,
                         f"HELD {len(changes)} change(s): portal writes untrusted")
        return {"ok": True, "pulled": len(changes), "applied": 0, "held": len(changes)}
    applied, change_ids = 0, []
    for c in changes:
        if not isinstance(c, dict):
            continue
        r = set_member(str(c.get("flat_id", "")), bool(int(c.get("requested_member", 0) or 0)))
        # ACK ON SUCCESS ONLY: a membership change that did not apply stays pending and is retried, so
        # WordPress never marks it synced while Mongoose's roll (and thus vote quorum) never changed.
        if r.get("ok"):
            applied += 1
            if c.get("change_id") is not None:
                change_ids.append(c.get("change_id"))
    if change_ids:
        _wp_call("POST", _profile.wp_route("/sync/member-changes-ack"), json_body={"change_ids": change_ids})
    _record_sync_run("pull_member_changes", True, f"pulled={len(changes)} applied={applied}")
    return {"ok": True, "pulled": len(changes), "applied": applied}


def _portal_writes_trusted() -> bool:
    """WordPress is an internet-facing surface that can be compromised, so data pulled FROM it must not
    silently write the money ledger or membership. Auto-apply is OFF unless MONGOOSE_PORTAL_TRUST_WRITES
    is explicitly enabled; otherwise pulled marks are held for the owner to confirm. Defense against a
    compromised WP injecting fake payments (the 2026-07 breach vector)."""
    import os as _os
    return (_os.getenv("MONGOOSE_PORTAL_TRUST_WRITES", "off") or "").strip().lower() in ("on", "true", "1")


def pull_contact_changes() -> dict[str, Any]:
    """Pull leaseholder CONTACT edits queued on WordPress (from the Flats details editor) and apply them
    to the RTM records. GATED like every other portal write: unless MONGOOSE_PORTAL_TRUST_WRITES is on the
    edits are HELD (logged, not written), because contact details drive who receives correspondence and a
    compromised WordPress must not silently redirect a leaseholder's post/email. set_flat_contact validates
    each field and only writes what actually changed."""
    from . import rtm_management as M
    res = _wp_call("GET", _profile.wp_route("/sync/contact-changes"))
    if not res.get("ok"):
        _record_sync_run("pull_contact_changes", False, res.get("error") or "pull failed")
        return {"ok": False, "error": res.get("error"), "applied": 0}
    data = res.get("data") or {}
    changes = data.get("changes") or []
    if not isinstance(changes, list):
        changes = []
    if changes and not _portal_writes_trusted():
        _record_sync_run("pull_contact_changes", True,
                         f"HELD {len(changes)} contact edit(s): portal writes untrusted")
        return {"ok": True, "pulled": len(changes), "applied": 0, "held": len(changes)}
    applied, change_ids = 0, []
    for c in changes:
        if not isinstance(c, dict):
            continue
        fid = str(c.get("flat_id") or "")
        fields = c.get("fields") if isinstance(c.get("fields"), dict) else {}
        try:
            r = M.set_flat_contact(fid, fields)
        except Exception as e:
            _record_sync_run("pull_contact_changes", False, f"change {c.get('change_id')}: {e}")
            continue  # leave PENDING; retried next pull
        # ACK ON SUCCESS ONLY (ok even when nothing changed, so a re-sent identical edit clears).
        if r.get("ok"):
            applied += 1
            if c.get("change_id") is not None:
                change_ids.append(c.get("change_id"))
    if change_ids:
        _wp_call("POST", _profile.wp_route("/sync/contact-changes-ack"),
                 json_body={"change_ids": change_ids})
    _record_sync_run("pull_contact_changes", True, f"pulled={len(changes)} applied={applied}")
    return {"ok": True, "pulled": len(changes), "applied": applied}


def pull_payment_marks() -> dict[str, Any]:
    """Pull director-requested 'mark paid' actions queued on WordPress. Auto-recording is GATED: unless
    MONGOOSE_PORTAL_TRUST_WRITES is on, pulled marks are HELD (logged, not written) so a compromised
    WordPress cannot inject payments into the ledger. record_payment is idempotent when it does run."""
    from . import rtm_management as M
    res = _wp_call("GET", _profile.wp_route("/sync/payment-marks"))
    if not res.get("ok"):
        _record_sync_run("pull_payment_marks", False, res.get("error") or "pull failed")
        return {"ok": False, "error": res.get("error"), "applied": 0}
    data = res.get("data") or {}
    marks = data.get("marks") or []
    if marks and not _portal_writes_trusted():
        # Held, not applied: a compromised WordPress must not be able to write the ledger unattended.
        _record_sync_run("pull_payment_marks", True,
                         f"HELD {len(marks)} mark(s): portal writes untrusted (set MONGOOSE_PORTAL_TRUST_WRITES=on to auto-apply)")
        return {"ok": True, "pulled": len(marks), "applied": 0, "held": len(marks)}
    if not isinstance(marks, list):
        marks = []
    applied, mark_ids = 0, []
    for m in marks:
        if not isinstance(m, dict):
            continue
        try:
            amt = m.get("amount")
            # WordPress sends amount as a formatted money string ("£1,075.16"); record_payment's own
            # parser handles that. float() here raised on the £/comma, so EVERY mark was pulled but never
            # recorded (applied=0) and stayed pending forever , the "marked paid but nothing updated" bug.
            r = M.record_payment(
                flat=str(m.get("flat_id", "")),
                document_number=str(m.get("document_number") or ""),
                amount=(amt if amt not in (None, "", "0", 0) else None),
                received_date=str(m.get("received_date") or ""),
                method="Bank transfer",
                note=f"Queued from WordPress by {m.get('requested_by') or 'director'}",
            )
        except Exception as e:  # a bad single mark must not stall the whole pull
            _record_sync_run("pull_payment_marks", False, f"mark {m.get('mark_id')}: {e}")
            continue  # leave it PENDING so the next pull retries it, never silently drop a payment
        # ACK ON SUCCESS ONLY: a mark that did not actually record stays pending and gets retried, so
        # WordPress can never show 'applied' for a payment the ledger never captured (silent money loss).
        if r.get("ok") or r.get("already_paid"):
            applied += 1
            if m.get("mark_id") is not None:
                mark_ids.append(m.get("mark_id"))
            # Generate the receipt PDF now, so a WordPress mark-paid never leaves a receipt showing
            # "no PDF yet" (the chat path already does this; this path didn't). Best-effort; a PDF hiccup
            # must not stall the pull or block the ack.
            try:
                generate_receipt_pack(flat_id=str(m.get("flat_id", "")),
                                      document_number=str(m.get("document_number") or ""))
            except Exception:
                pass
    if mark_ids:
        _wp_call("POST", _profile.wp_route("/sync/payment-marks-ack"),
                 json_body={"mark_ids": mark_ids})
    if applied:
        # Push the freshly-generated receipt PDFs and a snapshot carrying their keys, synchronously, so the
        # portal shows Paid + a working Download PDF on the very next refresh (not on the off-thread mirror).
        try:
            publish_receipt_pdfs()
        except Exception:
            pass
        try:
            publish_portal_snapshot(full=True)
        except Exception:
            pass
    _record_sync_run("pull_payment_marks", True, f"pulled={len(marks)} applied={applied}")
    return {"ok": True, "pulled": len(marks), "applied": applied}


def pull_vote_casts() -> dict[str, Any]:
    """Pull member vote casts held by WordPress, record them, and mark them synced.

    WordPress stores each member's cast (keyed by their flat_id) as they vote on the portal.
    Mongoose sits behind CGNAT so WordPress cannot push; this pull mirrors pull_pending_claims.
    Eligibility is re-checked in rtm_votes.ingest_casts against the persisted membership flag, so a
    cast from a non-member is recorded nowhere even if WordPress somehow forwarded it."""
    from .rtm_votes import ingest_casts
    res = _wp_call("GET", _profile.wp_route("/sync/vote-casts"))
    if not res.get("ok"):
        _record_sync_run("pull_vote_casts", False, res.get("error") or "pull failed")
        return {"ok": False, "error": res.get("error"), "accepted": 0, "rejected": 0}
    data = res.get("data") or {}
    casts = data.get("casts", data if isinstance(data, list) else [])
    if not isinstance(casts, list):
        casts = []
    ingested = ingest_casts(casts)
    # ACK ONLY WHEN FULLY PROCESSED: if any cast hit a TRANSIENT error, leave the whole batch pending so
    # the next pull retries it (cast_vote is idempotent per flat), rather than silently losing a vote.
    cast_ids = [c.get("cast_id") for c in casts if isinstance(c, dict) and c.get("cast_id") is not None]
    if cast_ids and ingested.get("ok"):
        _wp_call("POST", _profile.wp_route("/sync/vote-casts-synced"),
                 json_body={"cast_ids": cast_ids})
    _record_sync_run("pull_vote_casts", bool(ingested.get("ok")),
                     f"pulled={len(casts)} accepted={ingested['accepted']} rejected={ingested['rejected']} errored={ingested.get('errored', 0)}")
    return {"ok": bool(ingested.get("ok")), "pulled": len(casts), **ingested}


# ── Claim processing ──────────────────────────────────────────────────────────

def pull_pending_claims() -> dict[str, Any]:
    """Pull encrypted pending claim records from WordPress.

    WordPress stores only encrypted_payload + safe metadata; raw payment references and
    document numbers are never stored there and are never logged here."""
    res = _wp_call("GET", _profile.wp_route("/sync/pending-claims"))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "pending": [], "pulled_count": 0}
    data = res.get("data") or {}
    pending = data.get("claims", data if isinstance(data, list) else [])
    if not isinstance(pending, list):
        pending = []
    return {"ok": True, "pending": pending, "pulled_count": len(pending)}


def _flat_email_map() -> dict[str, str]:
    """flat_id (UPPER) -> leaseholder email (lower) used to auto-verify a claim's submitted email.

    Source of truth is the RTM records themselves (rtm_mgmt_flats.leaseholder_email), so the
    auto-approve path stays in sync with the same data that drives demands and the portal, with no
    duplicated email list to maintain. RTM_PORTAL_FLAT_EMAILS (JSON in .env) still works and OVERRIDES
    the DB per flat, for a quick correction without a data edit. A flat with no email on file simply
    never auto-approves: its claim goes to the director review queue, which is the safe default."""
    out: dict[str, str] = {}
    try:
        from ..db import connect
        with connect() as con:
            for r in con.execute(
                "SELECT flat_id, leaseholder_email FROM rtm_mgmt_flats "
                "WHERE leaseholder_email IS NOT NULL AND TRIM(leaseholder_email) != ''"):
                out[str(r["flat_id"]).upper()] = str(r["leaseholder_email"]).strip().lower()
    except Exception:
        pass
    import json as _json
    raw = os.environ.get("RTM_PORTAL_FLAT_EMAILS", "").strip()
    if raw:
        try:
            d = _json.loads(raw)
            if isinstance(d, dict):
                for k, v in d.items():
                    out[str(k).upper()] = str(v).strip().lower()
        except Exception:
            pass
    return out


def _validate_claim(*, payment_reference: str, document_number: str,
                    submitted_name: str, submitted_email: str) -> dict[str, Any]:
    """Validate a single claim against RTM management data. Local only; never calls WP and
    never logs the raw payment_reference or document_number.

    Decisions (spec section 6.2):
      approve_email_verification_required: matched flat + known email matches
      director_review_required: matched flat, email/name unverified
      reject: no match"""
    _ = submitted_name  # names vary legitimately; the email map is the verification signal
    from ..db import connect
    ref = (payment_reference or "").strip()
    doc = (document_number or "").strip()
    if not ref or not doc:
        return {"decision": "reject", "flat_id": None, "reason_code": "missing_fields"}
    # Case-INSENSITIVE, whitespace-trimmed match. The reference (e.g. MORGAN) and document number
    # (e.g. 26-TO-DEC/1ECF1R0) are printed in upper case on the demand, but a leaseholder retyping them
    # should not be rejected for typing "morgan" or a stray space. COLLATE NOCASE compares without case.
    with connect() as con:
        frow = con.execute("SELECT flat_id FROM rtm_mgmt_flats WHERE payment_reference = ? COLLATE NOCASE",
                           (ref,)).fetchone()
        if not frow:
            return {"decision": "reject", "flat_id": None, "reason_code": "reference_no_match"}
        flat_id = frow["flat_id"]
        drow = con.execute("SELECT 1 FROM rtm_mgmt_demands WHERE document_number = ? COLLATE NOCASE AND flat_id = ?",
                           (doc, flat_id)).fetchone()
    if not drow:
        return {"decision": "reject", "flat_id": None, "reason_code": "document_no_match"}
    known = _flat_email_map().get(str(flat_id).upper(), "")
    if known and known == (submitted_email or "").strip().lower():
        return {"decision": "approve_email_verification_required", "flat_id": flat_id,
                "reason_code": "matched_known_email"}
    return {"decision": "director_review_required", "flat_id": flat_id,
            "reason_code": "email_unverified"}


def push_claim_decisions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Push claim decisions back to WordPress. reason_code stays internal; a rejected user only
    ever sees CLAIM_FAILURE_MESSAGE (rendered by the plugin)."""
    pushed = 0
    errors: list[str] = []
    for d in decisions:
        cid = d.get("claim_id")
        if cid is None:
            continue
        body = {"decision": d.get("decision"), "flat_id": d.get("flat_id"),
                "reason_code": d.get("reason_code"),
                "failure_message": CLAIM_FAILURE_MESSAGE if d.get("decision") == "reject" else ""}
        res = _wp_call("POST", _profile.wp_route(f"/sync/claim-decision/{cid}"), json_body=body)
        if res.get("ok"):
            pushed += 1
        else:
            errors.append(f"claim {cid}: {res.get('error')}")
    return {"ok": not errors, "pushed": pushed, "errors": errors}


def process_pending_claims() -> dict[str, Any]:
    """Decrypt + validate all pending claims and push decisions back to WordPress.
    Called by the scheduler on a slow cadence. Logs counts only, never claim contents."""
    pulled = pull_pending_claims()
    if not pulled.get("ok"):
        _record_sync_run("process_claims", False, pulled.get("error") or "pull failed")
        return {"ok": False, "error": pulled.get("error"), "processed": 0}
    pending = pulled.get("pending", [])
    decisions: list[dict[str, Any]] = []
    provisions: list[dict[str, Any]] = []
    counts = {"approved": 0, "director_review": 0, "rejected": 0, "undecryptable": 0}
    for claim in pending:
        cid = claim.get("id")
        dec = decrypt_claim_payload(str(claim.get("encrypted_payload_b64")
                                        or claim.get("encrypted_payload") or ""))
        if not dec.get("ok"):
            counts["undecryptable"] += 1
            decisions.append({"claim_id": cid, "decision": "director_review_required",
                              "flat_id": None, "reason_code": "undecryptable"})
            continue
        c = dec["claim"] or {}
        v = _validate_claim(payment_reference=str(c.get("payment_reference", "")),
                            document_number=str(c.get("document_number", "")),
                            submitted_name=str(c.get("name", "")),
                            submitted_email=str(c.get("email", "")))
        decisions.append({"claim_id": cid, **v})
        if v["decision"].startswith("approve"):
            counts["approved"] += 1
            # Provision the leaseholder's WordPress account to the email ON FILE (the verified match),
            # never the submitted email, so an approved claim actually creates a login. flat_id is the
            # validated one from _validate_claim.
            fid = v.get("flat_id")
            known = _flat_email_map().get(str(fid).upper(), "") if fid else ""
            if known:
                # Name comes from the RTM records for the matched flat, never from the claim: the
                # sign-up no longer asks for a name (an unverified typed name could be anything).
                on_file_name = _flat_name_map().get(str(fid).upper(), "")
                provisions.append({"email": known, "flat_id": fid, "name": on_file_name})
        elif v["decision"] == "reject":
            counts["rejected"] += 1
            # Auto-rejected (wrong reference / document number): close the loop so the applicant is not
            # left waiting after "we will email you". Best-effort courtesy email; never reveals the
            # reason (that would leak which references are valid). Uses the email they submitted.
            _rej_email = str(c.get("email", "")).strip()
            if _rej_email:
                _wp_call("POST", _profile.wp_route("/portal/claim-notify"),
                         json_body={"email": _rej_email, "kind": "rejected"})
        else:
            counts["director_review"] += 1
            # Hold it for the owner to approve/reject in the Cockpit. Persist the decrypted essentials
            # now: once WordPress marks this claim 'director_review' it is no longer served to the pull,
            # so this local row is the only way to action it later. Idempotent on claim_id.
            if cid is not None:
                _record_review_claim(claim_id=int(cid), flat_id=str(v.get("flat_id") or ""),
                                     name=str(c.get("name", "")), email=str(c.get("email", "")),
                                     reason_code=str(v.get("reason_code", "")),
                                     submitted_at=str(claim.get("created_at", "")))
    push = push_claim_decisions(decisions) if decisions else {"ok": True, "pushed": 0}
    prov = _push_leaseholder_provisions(provisions) if provisions else {"provisioned": 0, "errors": []}
    ok = bool(push.get("ok")) and not prov.get("errors")
    _record_sync_run("process_claims", ok,
                     f"processed={len(decisions)} approved={counts['approved']} "
                     f"provisioned={prov.get('provisioned', 0)} "
                     f"review={counts['director_review']} rejected={counts['rejected']}")
    return {"ok": ok, "processed": len(decisions), **counts, "pushed": push.get("pushed", 0),
            "provisioned": prov.get("provisioned", 0),
            "errors": push.get("errors", []) + prov.get("errors", [])}


def _push_leaseholder_provisions(provisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Create/refresh the WordPress leaseholder account for each approved claim. The plugin's
    /portal/provision endpoint makes the account, binds it to the flat, and emails a set-password
    link so the leaseholder can log in. HMAC-verified, admin-gated, same as every other sync call."""
    done, errs, email_failed = 0, [], []
    for p in provisions:
        res = _wp_call("POST", _profile.wp_route("/portal/provision"), json_body=p)
        if res.get("ok"):
            done += 1
            # The account exists, but the plugin now reports whether the set-password email actually
            # sent. If it did NOT, the leaseholder got no login link: flag it so onboarding is not
            # silently "done" (the RTM can re-send) rather than trusting account-created == onboarded.
            data = res.get("data") or {}
            if data.get("email_sent") is False:
                email_failed.append(str(p.get("flat_id") or ""))
        else:
            errs.append(f"provision {p.get('flat_id')}: {res.get('error')}")
    out = {"provisioned": done, "errors": errs}
    if email_failed:
        out["email_not_sent"] = email_failed
    return out


# ── Manual access-request review (owner approves what could not be auto-verified) ─────

def _record_review_claim(*, claim_id: int, flat_id: str, name: str, email: str,
                         reason_code: str, submitted_at: str) -> None:
    """Persist (idempotently) an access request that needs the owner's decision."""
    _ensure_portal_tables()
    from ..db import connect, utc_now_iso
    with connect() as con:
        con.execute(
            "INSERT INTO portal_review_claims(claim_id, flat_id, name, email, reason_code, "
            "submitted_at, status, created_at) VALUES(?,?,?,?,?,?, 'pending', ?) "
            "ON CONFLICT(claim_id) DO UPDATE SET flat_id=excluded.flat_id, name=excluded.name, "
            "email=excluded.email, reason_code=excluded.reason_code WHERE status='pending'",
            (int(claim_id), flat_id, name, email, reason_code, submitted_at, utc_now_iso()))
        con.commit()


def list_review_claims(status: str = "pending") -> list[dict[str, Any]]:
    """Access requests awaiting the owner's decision (or a given status), newest first."""
    _ensure_portal_tables()
    from ..db import connect
    with connect() as con:
        rows = con.execute(
            "SELECT claim_id, flat_id, name, email, reason_code, submitted_at, status, created_at, "
            "resolved_at FROM portal_review_claims WHERE status=? ORDER BY claim_id DESC",
            (status,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        fid = str(d.get("flat_id") or "").upper()
        # Show the leaseholder name we hold for that flat, so the owner can sanity-check the request.
        d["on_file_name"] = _flat_name_map().get(fid, "")
        d["on_file_email"] = _flat_email_map().get(fid, "")
        out.append(d)
    return out


def _flat_name_map() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        from ..db import connect
        with connect() as con:
            for r in con.execute("SELECT flat_id, leaseholder_name FROM rtm_mgmt_flats"):
                out[str(r["flat_id"]).upper()] = str(r["leaseholder_name"] or "")
    except Exception:
        pass
    return out


def approve_review_claim(claim_id: int) -> dict[str, Any]:
    """Owner approves a held access request: provision the WordPress account (set-password email
    included) and mark the claim approved on both sides. Provisions to the email the requester gave
    (they receive the set-password link there); the owner is vouching for it by approving."""
    _ensure_portal_tables()
    from ..db import connect, utc_now_iso
    with connect() as con:
        row = con.execute("SELECT * FROM portal_review_claims WHERE claim_id=?", (int(claim_id),)).fetchone()
    if not row:
        return {"ok": False, "error": "unknown claim"}
    if row["status"] != "pending":
        return {"ok": False, "error": f"already {row['status']}"}
    flat_id, email = str(row["flat_id"]), str(row["email"])
    if not email or not flat_id:
        return {"ok": False, "error": "claim is missing an email or flat"}
    # Name from the RTM records for the flat, not the claim (sign-up does not collect a name).
    name = _flat_name_map().get(flat_id.upper(), "") or str(row["name"] or "")
    # By approving, the owner confirms this email IS the leaseholder for this flat. Record it as the
    # flat's on-file email so the plugin's fail-closed entitlement check (C1) recognises this account on
    # its very next render , without this, a flat that had no email on file would provision the account
    # and then immediately deny it. Then republish so WordPress's snapshot carries the new email.
    try:
        from ..db import connect as _con2
        with _con2() as con:
            con.execute("UPDATE rtm_mgmt_flats SET leaseholder_email=?, updated_at=? WHERE flat_id=?",
                        (email.strip().lower(), utc_now_iso(), flat_id))
            con.commit()
    except Exception:
        pass
    prov = _push_leaseholder_provisions([{"email": email, "flat_id": flat_id, "name": name}])
    if prov.get("errors"):
        return {"ok": False, "error": "; ".join(prov["errors"])[:200]}
    try:
        publish_portal_snapshot(full=True)  # so the plugin's C1 check sees the updated on-file email now
    except Exception:
        pass
    # Record the approval decision on the WordPress claim row too, so its status is not left at review.
    push_claim_decisions([{"claim_id": int(claim_id), "decision": "approve_manual", "flat_id": flat_id}])
    with connect() as con:
        con.execute("UPDATE portal_review_claims SET status='approved', resolved_at=? WHERE claim_id=?",
                    (utc_now_iso(), int(claim_id)))
        con.commit()
    _record_sync_run("approve_claim", True, f"flat={flat_id} provisioned=1")
    return {"ok": True, "flat_id": flat_id, "email": email, "provisioned": prov.get("provisioned", 0)}


def reject_review_claim(claim_id: int, reason: str = "") -> dict[str, Any]:
    """Owner rejects a held access request: mark it rejected on both sides. No account is created."""
    _ensure_portal_tables()
    from ..db import connect, utc_now_iso
    with connect() as con:
        row = con.execute("SELECT status, email FROM portal_review_claims WHERE claim_id=?", (int(claim_id),)).fetchone()
    if not row:
        return {"ok": False, "error": "unknown claim"}
    if row["status"] != "pending":
        return {"ok": False, "error": f"already {row['status']}"}
    push_claim_decisions([{"claim_id": int(claim_id), "decision": "reject", "flat_id": "",
                           "failure_message": (reason or "not approved")[:200]}])
    # Courtesy email so the applicant is not left waiting after "we will email you". Best-effort; never
    # reveals the reason. The email address is the one they submitted (held locally for this decision).
    applicant_email = str(row["email"] or "")
    if applicant_email:
        _wp_call("POST", _profile.wp_route("/portal/claim-notify"),
                 json_body={"email": applicant_email, "kind": "rejected"})
    with connect() as con:
        con.execute("UPDATE portal_review_claims SET status='rejected', resolved_at=? WHERE claim_id=?",
                    (utc_now_iso(), int(claim_id)))
        con.commit()
    _record_sync_run("reject_claim", True, f"claim={claim_id}")
    return {"ok": True, "claim_id": int(claim_id)}


# ── Portal issues → RTM intake ────────────────────────────────────────────────

def pull_portal_issues() -> dict[str, Any]:
    """Pull submitted leaseholder issues from WordPress into RTM intake. Payment issues create
    an intake case only and NEVER alter the ledger."""
    res = _wp_call("GET", _profile.wp_route("/sync/issues"))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "synced_count": 0, "issues": []}
    data = res.get("data") or {}
    issues = data.get("issues", data if isinstance(data, list) else [])
    if not isinstance(issues, list):
        issues = []
    synced = 0
    errors: list[str] = []
    for it in issues:
        r = intake_portal_issue(
            flat_id=str(it.get("flat_id", "")), category=str(it.get("category", "other")),
            title=str(it.get("title", "Portal issue")), description=str(it.get("description", "")),
            urgency=str(it.get("urgency", "routine")), location=str(it.get("location", "")),
            portal_reference=str(it.get("reference", it.get("id", ""))),
            submitted_at=str(it.get("submitted_at", "")),
            payment_date=it.get("payment_date"), amount_paid=it.get("amount_paid"),
            reference_used=it.get("reference_used"))
        if r.get("ok"):
            synced += 1
            iid = it.get("id")
            if iid is not None:
                _wp_call("POST", _profile.wp_route(f"/sync/issue-synced/{iid}"), json_body={"synced": True})
        else:
            errors.append(str(r.get("error"))[:120])
    _record_sync_run("pull_issues", not errors, f"synced={synced}")
    return {"ok": not errors, "synced_count": synced, "issue_count": len(issues), "errors": errors}


def intake_portal_issue(*, flat_id: str, category: str, title: str, description: str,
                         urgency: str = "routine", location: str = "",
                         portal_reference: str = "", submitted_at: str = "",
                         payment_date: str | None = None, amount_paid: str | None = None,
                         reference_used: str | None = None) -> dict[str, Any]:
    """Create an RTM intake case from a portal issue submission.

    CRITICAL: Payment issues do NOT alter the ledger. They create an intake case only.

    DELTA (stub → real):
      - Call rtm_intake.intake_email(sender=flat_id, subject=title, body=description, ...)
        with kind="portal_issue", source="leaseholder_portal"
      - For payment issues: include payment_date/amount/reference in the case body only
       : NEVER create a ledger entry, NEVER mark payment as received
      - Return {ok, case_id, intake_id}
    """
    # Normalise the category. The public portal uses leaseholder-friendly labels (maintenance,
    # financial, property_sale, question, contact) that do NOT match the intake's internal set. This
    # mismatch silently DROPPED every leaseholder message: intake rejected the category, the row was
    # never acked, and it retried-and-failed forever while the leaseholder saw a "sent" confirmation.
    # A leaseholder message must NEVER be lost on a category technicality, so map known labels and
    # default anything unrecognised to "other" rather than failing.
    valid_categories = {"building", "payment", "document", "portal", "other"}
    _category_map = {
        "maintenance": "building", "repair": "building", "building": "building",
        "financial": "payment", "payment": "payment",
        "property_sale": "other", "sale": "other", "question": "other",
        "contact": "other", "general": "other", "document": "document", "portal": "portal",
    }
    raw_category = (category or "").strip().lower()
    category = _category_map.get(raw_category, raw_category if raw_category in valid_categories else "other")

    # Payment issue safety gate: log but never touch ledger
    if category.lower() == "payment":
        _ = payment_date, amount_paid, reference_used  # record-only, no ledger action

    try:
        from .rtm_intake import intake_email
        # intake_email takes (sender, subject, body, source_message_id, received_at, kind).
        # There is no flat_id kwarg: the flat is encoded in sender and body so it threads correctly
        # and is visible in the case. kind="portal_issue" tags the source for downstream filtering.
        result = intake_email(
            sender=f"portal+{flat_id}@{_site_domain()}",
            subject=f"[Portal:{category.upper()}] {title}",
            body=(
                f"Flat: {flat_id}\n"
                f"Category: {category}\n"
                f"Urgency: {urgency}\n"
                f"Location: {location}\n"
                f"Portal reference: {portal_reference}\n"
                f"Submitted: {submitted_at}\n\n"
                f"{description}"
                + (f"\n\nPayment details (for reference only: not a confirmation of payment):\n"
                   f"  Date: {payment_date}\n  Amount: {amount_paid}\n  Reference: {reference_used}"
                   if category.lower() == "payment" and any([payment_date, amount_paid, reference_used])
                   else "")
            ),
            source_message_id=f"portal:{portal_reference}" if portal_reference else "",
            received_at=submitted_at,
            kind="portal_issue",
        )
        return {"ok": True, "flat_id": flat_id, "category": category, "intake_result": result}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── PDF handling ──────────────────────────────────────────────────────────────

def _index_demand_pdf_attachment(document_number: str, pdf_path: str) -> None:
    """Index a generated demand PDF into the attachments store so unified_search surfaces it with a
    retrieve_attachment handle (fixes the 'parent email, no attachment handle' dead-end). Best-effort:
    NEVER raises into the caller, because demand generation must not break if indexing fails. Idempotent
    (stable source_message_id => index_attachment updates rather than duplicates)."""
    try:
        from ..db import connect
        with connect() as con:
            row = con.execute("SELECT flat_id, period FROM rtm_mgmt_demands WHERE document_number=?",
                              (document_number,)).fetchone()
        flat = (row[0] if row else "") or ""
        period = (row[1] if row else "") or ""
        p = Path(pdf_path).expanduser()
        raw = p.read_bytes()
        try:
            from .content_extract import extract_text
            text = (extract_text(p) or "")[:20000]
        except Exception:
            text = ""
        import base64
        from .attachments import index_attachment
        index_attachment(
            source="rtm_demand", source_message_id=f"rtm-demand-{document_number}",
            filename=(f"Demand_{flat}_{period}.pdf").replace("__", "_"),
            mime_type="application/pdf", size_bytes=len(raw),
            subject=f"Service charge demand {document_number} ({flat} {period})".strip(),
            sender=f"{_profile.rtm_name()} RTM", content_text=text,
            payload_b64=base64.b64encode(raw).decode("ascii"))
    except Exception:
        pass


def backfill_demand_pdf_attachments() -> dict[str, Any]:
    """One-time: index every already-generated demand PDF into the attachments store so past demands
    become searchable/retrievable through the connector. Safe to re-run (idempotent)."""
    from ..db import connect
    with connect() as con:
        rows = con.execute("SELECT document_number, pdf_storage_path FROM portal_demand_pdfs").fetchall()
    n = 0
    for r in rows:
        docnum, path = r[0], r[1]
        if path and Path(path).expanduser().exists():
            _index_demand_pdf_attachment(docnum, path)
            n += 1
    return {"ok": True, "indexed": n, "total": len(rows)}


def attach_demand_pdf(*, document_number: str, path: str) -> dict[str, Any]:
    """Attach a demand PDF for the portal: copy to PC_PORTAL_PRIVATE_DIR (outside web root)
    and record pdf_key + sha256. pdf_storage_path is server-side only and never returned."""
    _ensure_portal_tables()
    src = Path(path).expanduser()
    if not src.exists():
        return {"ok": False, "error": f"File not found: {path}"}
    from ..db import connect
    with connect() as con:
        d = con.execute("SELECT 1 FROM rtm_mgmt_demands WHERE document_number=?", (document_number,)).fetchone()
    if not d:
        return {"ok": False, "error": f"unknown demand {document_number}"}
    env = _portal_env()
    # Anchor to an ABSOLUTE data dir: default to the ~/.mongoose data home, never the relative "data"
    # (which resolved against the process cwd and split receipts between ~/.mongoose/data and app/data).
    _data_dir = os.environ.get("MONGOOSE_DATA_DIR") or str(Path.home() / ".mongoose" / "data")
    priv_dir = Path(env.get("PC_PORTAL_PRIVATE_DIR") or (Path(_data_dir) / "portal" / "pdfs")).expanduser()
    priv_dir.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256(src.read_bytes()).hexdigest()
    pdf_key = secrets.token_urlsafe(32)
    dest = priv_dir / f"{pdf_key}.pdf"
    dest.write_bytes(src.read_bytes())
    from ..db import utc_now_iso
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO portal_demand_pdfs(document_number, pdf_key, pdf_sha256, pdf_storage_path, created_at) VALUES(?,?,?,?,?)",
            (document_number, pdf_key, sha256, str(dest), utc_now_iso()))
        con.commit()
    _index_demand_pdf_attachment(document_number, str(dest))  # searchable via the connector; best-effort
    return {"ok": True, "document_number": document_number, "pdf_key": pdf_key, "pdf_sha256": sha256}


def _receipt_no_for(document_number: str, flat_id: str) -> str:
    """A receipt number that is UNIQUE PER PERIOD, derived from the demand it pays.

    The old form was f"RCPT-26-{flat}" , a hardcoded year and no period, so every period produced the
    same string for a flat. A demand number looks like "26-TO-DEC/1ECF1R0", whose left part ("26-TO-DEC")
    already identifies year+period, so "RCPT-26-TO-DEC-1ECF1" is unique per period and human-readable.
    Falls back to the flat alone only when there is no demand number to derive from.
    """
    doc = (document_number or "").strip()
    fid = (flat_id or "").strip().upper()
    head = doc.split("/", 1)[0].strip() if "/" in doc else ""
    return f"RCPT-{head}-{fid}" if head else f"RCPT-{fid}"


def attach_receipt_pdf(*, flat_id: str, receipt_no: str, path: str, period: str = "",
                       amount: str = "", received_date: str = "", document_number: str = "") -> dict[str, Any]:
    """Attach a payment RECEIPT PDF for the portal, mirroring attach_demand_pdf: copy to the private dir
    (outside web root) and record pdf_key + sha256. The leaseholder downloads it flat-gated, exactly like
    a demand PDF. pdf_storage_path is server-side only and never returned.

    IDENTITY IS THE DEMAND. Pass document_number and the receipt is keyed to the demand it pays: a
    re-issue for the SAME demand replaces that row in place, while a different period's demand always
    gets its own row. Without this, receipt_no alone (historically RCPT-<yy>-<flat>, no period) repeated
    every period and INSERT OR REPLACE silently destroyed the previous period's receipt."""
    _ensure_portal_tables()
    src = Path(path).expanduser()
    if not src.exists():
        return {"ok": False, "error": f"File not found: {path}"}
    flat_id = (flat_id or "").strip().upper()
    receipt_no = (receipt_no or "").strip()
    if not flat_id or not receipt_no:
        return {"ok": False, "error": "flat_id and receipt_no are required"}
    from ..db import connect
    env = _portal_env()
    # Anchor to an ABSOLUTE data dir: default to the ~/.mongoose data home, never the relative "data"
    # (which resolved against the process cwd and split receipts between ~/.mongoose/data and app/data).
    _data_dir = os.environ.get("MONGOOSE_DATA_DIR") or str(Path.home() / ".mongoose" / "data")
    priv_dir = Path(env.get("PC_PORTAL_PRIVATE_DIR") or (Path(_data_dir) / "portal" / "pdfs")).expanduser()
    priv_dir.mkdir(parents=True, exist_ok=True)
    sha256 = hashlib.sha256(src.read_bytes()).hexdigest()
    pdf_key = secrets.token_urlsafe(32)
    dest = priv_dir / f"{pdf_key}.pdf"
    dest.write_bytes(src.read_bytes())
    from ..db import utc_now_iso
    document_number = (document_number or "").strip()
    with connect() as con:
        # Re-issuing a receipt for the SAME demand updates that demand's existing row (whatever its
        # historical receipt_no), so we never leave two receipts for one demand; a different demand can
        # never collide onto it. Falls back to receipt_no keying only when no demand is supplied.
        existing = None
        if document_number:
            existing = con.execute("SELECT receipt_no FROM portal_receipt_pdfs WHERE document_number=?",
                                   (document_number,)).fetchone()
        if existing:
            receipt_no = existing["receipt_no"]
        con.execute(
            "INSERT OR REPLACE INTO portal_receipt_pdfs(receipt_no, flat_id, pdf_key, pdf_sha256, pdf_storage_path, period, amount, received_date, document_number, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (receipt_no, flat_id, pdf_key, sha256, str(dest), period, amount, received_date,
             document_number, utc_now_iso()))
        con.commit()
    return {"ok": True, "receipt_no": receipt_no, "flat_id": flat_id, "pdf_key": pdf_key,
            "document_number": document_number}


# ── Document generation (receipts + demand PDFs) ─────────────────────────────────
# The one canonical receipt-pack builder, matching the owner's supplied receipts exactly (validated byte-for-
# content against the Rahimi receipt): payment_receipt.docx -> PDF (soffice) with the flat's full 4-page
# demand appended. Used by auto-generate-on-payment AND the plugin "generate missing documents" pull.

def _fmt_long_date(iso: str) -> str:
    """ISO YYYY-MM-DD -> '13 July 2026' (matches the receipt/demand documents)."""
    try:
        from datetime import date as _date
        d = _date.fromisoformat(str(iso)[:10])
        try:
            return d.strftime("%-d %B %Y")
        except ValueError:  # non-GNU strftime
            return d.strftime("%d %B %Y").lstrip("0")
    except Exception:
        return str(iso or "")


def _receipt_period_label(period: str) -> str:
    """Map a stored demand period ('2026-H2') onto the document phrasing. Falls back to the raw period."""
    import re as _re
    m = _re.match(r"^(\d{4})-H([12])$", str(period or "").strip())
    if not m:
        return str(period or "")
    year, half = m.group(1), m.group(2)
    return f"the half-year ending {'31 December' if half == '2' else '30 June'} {year}"


def generate_receipt_pack(*, flat_id: str, document_number: str = "", force: bool = False) -> dict[str, Any]:
    """Build (and attach) the receipt-with-demand PDF for a flat's SETTLED demand, exactly matching the
    branded pack format. Idempotent: skips if a receipt is already stored for this flat (unless force).
    Only issues when the demand's outstanding is zero (a receipt states money already received). Returns
    {ok, receipt_no, ...} or {ok:False, reason}. Never raises to its caller: generation is best-effort so
    it can be hung off record_payment without ever blocking the payment itself."""
    _ensure_portal_tables()
    from ..db import connect
    from .rtm_management import list_demands, _get_flat  # local: avoid import cycle
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    try:
        # Resolve the demand: by document_number, else the flat's most recent settled demand.
        dem = None
        for d in list_demands(flat=fid):
            if document_number and d.get("document_number") != document_number:
                continue
            if int(d.get("outstanding_pence") or 0) <= 0 and int(d.get("amount_pence") or 0) > 0:
                dem = d
                break
        if not dem:
            return {"ok": False, "reason": "no_settled_demand", "flat_id": fid}
        doc = dem["document_number"]
        # Skip if a receipt for this flat already exists, unless force (backfill/regeneration).
        with connect() as con:
            existing = con.execute("SELECT receipt_no FROM portal_receipt_pdfs WHERE flat_id=?", (fid,)).fetchone()
            yr = con.execute("SELECT MAX(year) FROM rtm_mgmt_budget_lines").fetchone()
            btrow = con.execute(
                "SELECT bt.bank_date FROM rtm_mgmt_allocations al "
                "JOIN rtm_mgmt_bank_transactions bt ON bt.id = al.bank_transaction_id "
                "WHERE al.document_number=? ORDER BY bt.bank_date DESC LIMIT 1", (doc,)).fetchone()
        if existing and not force:
            return {"ok": True, "already_present": True, "flat_id": fid, "receipt_no": existing["receipt_no"]}
        year = str(yr[0]) if yr and yr[0] else "2027"
        period_label = _receipt_period_label(dem.get("period", ""))
        demand_date = _fmt_long_date(dem.get("issue_date", ""))
        due_date = _fmt_long_date(dem.get("due_date", ""))
        received_date = _fmt_long_date((btrow["bank_date"] if btrow else "") or dem.get("issue_date", ""))
        received_iso = str((btrow["bank_date"] if btrow else "") or dem.get("issue_date", ""))[:10]

        from .rtm_apportionment_docs import demand_docx_context, render_demand_docx
        from .rtm_templates import render_docx, to_pdf
        fl = _get_flat(fid) or {}
        ref = str(fl.get("payment_reference") or fid)
        # Reuse the demand's own merge context for name/greeting/premises/address, so the receipt page and
        # the appended demand page can never disagree (exactly how the owner's supplied receipts are built).
        dctx = demand_docx_context(flat_id=fid, year=year, period_label=period_label,
                                   demand_date=demand_date, due_date=due_date, doc_number=doc)
        rctx = {
            "issue_date": demand_date, "receipt_no": _receipt_no_for(doc, fid),
            "leaseholder_name": dctx["leaseholder_name"], "addr_line_1": dctx["addr_line_1"],
            "addr_line_2": dctx["addr_line_2"], "premises": dctx["premises"],
            "greeting_name": dctx["greeting_name"], "payer_name": dctx["leaseholder_name"],
            "payment_ref": dctx["payment_ref"], "period": period_label, "doc_number": doc,
            "amount_received": dctx["total_due"], "received_date": received_date,
            "method": "Bank transfer", "outstanding": "£0.00",
        }
        work = Path(os.environ.get("MONGOOSE_RTM_RECEIPTS_DIR")
                    or (Path(os.environ.get("MONGOOSE_DATA_DIR") or str(Path.home() / ".mongoose" / "data")) / "rtm_receipts")).expanduser()
        work.mkdir(parents=True, exist_ok=True)
        r_docx = work / f"_gen_receipt_{fid}.docx"
        d_docx = work / f"_gen_demand_{fid}.docx"
        rr = render_docx("payment_receipt", rctx, out_path=str(r_docx), validate=False)
        if not rr.get("ok"):
            return {"ok": False, "reason": "receipt_docx_failed", "detail": rr}
        rp = to_pdf(str(r_docx))
        if not rp.get("ok"):
            return {"ok": False, "reason": "receipt_pdf_failed", "detail": rp,
                    "hint": "LibreOffice/soffice is required to export the receipt to PDF."}
        dd = render_demand_docx(flat_id=fid, year=year, period_label=period_label, demand_date=demand_date,
                                due_date=due_date, doc_number=doc, out_path=str(d_docx))
        if not dd.get("ok"):
            return {"ok": False, "reason": "demand_docx_failed", "detail": dd}
        dp = to_pdf(str(d_docx))
        if not dp.get("ok"):
            return {"ok": False, "reason": "demand_pdf_failed", "detail": dp}
        from pypdf import PdfReader, PdfWriter
        w = PdfWriter()
        for p in PdfReader(rp["path"]).pages:
            w.add_page(p)
        for p in PdfReader(dp["path"]).pages:
            w.add_page(p)
        pack = work / f"{_receipt_no_for(doc, fid)}-{ref.title()}.pdf"
        with open(pack, "wb") as fh:
            w.write(fh)
        # Tidy the intermediates (docx + single-doc pdfs), keep only the named pack.
        for tmp in (r_docx, d_docx, Path(rp["path"]), Path(dp["path"])):
            try:
                if str(tmp) != str(pack):
                    tmp.unlink()
            except OSError:
                pass
        res = attach_receipt_pdf(flat_id=fid, receipt_no=_receipt_no_for(doc, fid), path=str(pack),
                                 period=period_label, amount=dctx["total_due"], received_date=received_iso,
                                 document_number=doc)
        res["generated"] = True
        return res
    except Exception as e:  # never propagate: generation is best-effort
        return {"ok": False, "reason": "exception", "error": str(e), "flat_id": fid}


def resolve_receipt_pdf(flat_id: str, document_number: str = "") -> str | None:
    """On-disk path to the BRANDED receipt PDF (the template-engine pack: the receipt page followed by
    the original demand), building it via generate_receipt_pack if it is not stored yet. This is the ONE
    source for 'download this receipt', so the download button gives the identical branded PDF that
    WordPress and email send. Replaces the retired reportlab renderer. Returns None if there is no
    settled demand to receipt or the render fails (e.g. LibreOffice missing)."""
    _ensure_portal_tables()
    from ..db import connect
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")

    def _stored() -> str | None:
        with connect() as con:
            r = con.execute("SELECT pdf_storage_path FROM portal_receipt_pdfs WHERE flat_id=? "
                            "ORDER BY created_at DESC LIMIT 1", (fid,)).fetchone()
        p = (r["pdf_storage_path"] if r else "") or ""
        return p if (p and Path(p).exists()) else None

    p = _stored()
    if p:
        return p
    gen = generate_receipt_pack(flat_id=fid, document_number=document_number or "")
    return _stored() if gen.get("ok") else None


def missing_documents() -> dict[str, Any]:
    """What the portal is missing, for the plugin's 'generate missing documents' panel:
    - receipts: a SETTLED demand (outstanding 0) whose flat has no stored receipt PDF.
    - demand_pdfs: a demand with no pushed PDF (no pdf_key).
    Read-only; drives the Missing -> Generating -> Ready indicator on the WordPress side."""
    _ensure_portal_tables()
    from ..db import connect
    from .rtm_management import list_demands
    with connect() as con:
        have_receipt = {r["flat_id"] for r in con.execute("SELECT DISTINCT flat_id FROM portal_receipt_pdfs").fetchall()}
        have_pdf = {r["document_number"] for r in con.execute("SELECT document_number FROM portal_demand_pdfs").fetchall()}
    receipts, demand_pdfs = [], []
    for d in list_demands():
        doc, fid = d.get("document_number"), d.get("flat_id")
        if doc not in have_pdf:
            demand_pdfs.append({"document_number": doc, "flat_id": fid, "period": d.get("period")})
        if int(d.get("amount_pence") or 0) > 0 and int(d.get("outstanding_pence") or 0) <= 0 and fid not in have_receipt:
            receipts.append({"document_number": doc, "flat_id": fid, "period": d.get("period")})
    return {"ok": True, "missing_receipts": receipts, "missing_demand_pdfs": demand_pdfs}


def _pdf_text(path: str) -> str:
    """Extract all text from a PDF, best-effort (empty string on any failure)."""
    try:
        try:
            from pypdf import PdfReader  # type: ignore
        except Exception:
            from PyPDF2 import PdfReader  # type: ignore
        return "".join((p.extract_text() or "") for p in PdfReader(path).pages)
    except Exception:
        return ""


def _demand_material_fields(text: str) -> dict[str, Any]:
    """The money-critical + identity fields of a demand PDF, for the regeneration-diff guard: the set of
    money amounts, the payment reference, the document number, and the addressee name."""
    import re
    amounts = sorted(set(re.findall(r"£[0-9,]+\.[0-9]{2}", text)))
    ref = re.search(r"reference\s+([A-Z0-9]+)", text)
    doc = re.search(r"Document number\s+(\S+)", text)
    addressee = re.search(r"Document number\s+\S+\s+(.+?)\n", text)
    return {"amounts": amounts,
            "payment_reference": ref.group(1) if ref else "",
            "document_number": doc.group(1) if doc else "",
            "addressee": (addressee.group(1).strip() if addressee else "")}


def _demand_material_diff(old_path: str, new_path: str) -> dict[str, Any]:
    """Compare two demand PDFs on their material fields. Returns a dict of {field: {old, new}} for every
    field that materially differs; empty dict means they are materially the same. The addressee is only
    compared when it was extractable from BOTH (so a flaky text extraction never causes a false block),
    but a genuine name change (e.g. individuals -> a company) is still caught."""
    import re
    old_m = _demand_material_fields(_pdf_text(old_path))
    new_m = _demand_material_fields(_pdf_text(new_path))
    diffs: dict[str, Any] = {}
    for k in ("amounts", "payment_reference", "document_number"):
        if old_m[k] != new_m[k]:
            diffs[k] = {"old": old_m[k], "new": new_m[k]}

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "")).strip().lower()

    if old_m["addressee"] and new_m["addressee"] and _norm(old_m["addressee"]) != _norm(new_m["addressee"]):
        diffs["addressee"] = {"old": old_m["addressee"], "new": new_m["addressee"]}
    return diffs


def regenerate_demand_pdf(*, document_number: str, force: bool = False) -> dict[str, Any]:
    """Rebuild a demand's PDF from the real engine and attach it (for a demand missing its pushed PDF).

    Regeneration guard: if a PDF is ALREADY stored for this demand, the freshly rendered one is compared
    against it on the money-critical + identity fields (amounts, payment reference, document number,
    addressee). On any material difference the replacement is REFUSED (the temp render is discarded and
    the stored PDF is left untouched) unless force=True, so a later template or data change can never
    silently alter a demand that has already gone out. Pass force=True to replace deliberately (that
    supersedes the version already issued)."""
    from ..db import connect
    from .rtm_apportionment_docs import render_demand_docx
    from .rtm_templates import to_pdf
    try:
        with connect() as con:
            d = con.execute("SELECT flat_id, period, issue_date, due_date FROM rtm_mgmt_demands WHERE document_number=?",
                            (document_number,)).fetchone()
            yr = con.execute("SELECT MAX(year) FROM rtm_mgmt_budget_lines").fetchone()
        if not d:
            return {"ok": False, "reason": "unknown_demand"}
        year = str(yr[0]) if yr and yr[0] else "2027"
        work = Path(os.environ.get("MONGOOSE_RTM_RECEIPTS_DIR")
                    or (Path(os.environ.get("MONGOOSE_DATA_DIR") or str(Path.home() / ".mongoose" / "data")) / "rtm_receipts")).expanduser()
        work.mkdir(parents=True, exist_ok=True)
        d_docx = work / f"_gen_demandonly_{d['flat_id']}.docx"
        dd = render_demand_docx(flat_id=d["flat_id"], year=year, period_label=_receipt_period_label(d["period"]),
                                demand_date=_fmt_long_date(d["issue_date"]), due_date=_fmt_long_date(d["due_date"]),
                                doc_number=document_number, out_path=str(d_docx))
        if not dd.get("ok"):
            return {"ok": False, "reason": "demand_docx_failed", "detail": dd}
        dp = to_pdf(str(d_docx))
        if not dp.get("ok"):
            return {"ok": False, "reason": "demand_pdf_failed", "detail": dp}
        # Guard: refuse to silently replace an already-stored demand PDF that materially differs.
        if not force:
            with connect() as con:
                prev = con.execute("SELECT pdf_storage_path FROM portal_demand_pdfs WHERE document_number=?",
                                   (document_number,)).fetchone()
            prev_path = prev["pdf_storage_path"] if prev else ""
            if prev_path and os.path.exists(prev_path):
                diffs = _demand_material_diff(prev_path, dp["path"])
                if diffs:
                    for tmp in (d_docx, Path(dp["path"])):
                        try:
                            tmp.unlink()
                        except OSError:
                            pass
                    return {"ok": False, "reason": "material_difference", "document_number": document_number,
                            "diffs": diffs,
                            "hint": "The regenerated demand differs from the stored one on the fields above. "
                                    "The stored PDF was left untouched. Re-run with force=True to replace it "
                                    "deliberately (this supersedes the version already issued)."}
        res = attach_demand_pdf(document_number=document_number, path=dp["path"])
        for tmp in (d_docx, Path(dp["path"])):
            try:
                tmp.unlink()
            except OSError:
                pass
        return res
    except Exception as e:
        return {"ok": False, "reason": "exception", "error": str(e)}


def ensure_missing_receipts() -> dict[str, Any]:
    """Backfill: generate a receipt for every settled demand whose flat has none. Returns what it made."""
    miss = missing_documents().get("missing_receipts", [])
    made = []
    for m in miss:
        r = generate_receipt_pack(flat_id=m["flat_id"], document_number=m["document_number"])
        if r.get("ok") and r.get("generated"):
            made.append(m["flat_id"])
    return {"ok": True, "generated": made, "count": len(made)}


def sync_wp_queues(*, publish: bool = True) -> dict[str, Any]:
    """Apply everything a director queued on WordPress (mark-paid, membership changes, document requests)
    and, if anything changed, republish so the results ride back. This is the ONE place the three queues
    are drained, shared by the scheduler timer AND the WordPress 'you have work' nudge, so a portal click
    takes effect on its own, no manual Cockpit pull. All calls degrade honestly if WordPress is down."""
    marks = pull_payment_marks()
    changes = pull_member_changes()
    contacts = pull_contact_changes()
    docs = pull_doc_requests()
    applied = (int(marks.get("applied", 0) or 0) + int(changes.get("applied", 0) or 0)
               + int(contacts.get("applied", 0) or 0) + int(docs.get("generated", 0) or 0))
    out: dict[str, Any] = {"ok": bool(marks.get("ok") and changes.get("ok") and contacts.get("ok") and docs.get("ok")),
                           "applied": applied, "payment_marks": marks,
                           "member_changes": changes, "contact_changes": contacts, "doc_requests": docs}
    if applied > 0 and publish:
        snap = publish_portal_snapshot(full=True)
        # The director summary (paid-vs-demanded %, balances) also moves when a payment is applied, and it
        # publishes on a SEPARATE endpoint from the snapshot. Refresh it here too, otherwise the portal's
        # headline percentage stays stale after a mark-paid (it showed 31% while the ledger was already 36%).
        dirsum = publish_director_summary() if snap.get("ok") else {"ok": False}
        out["published"] = {"ok": snap.get("ok"),
                            "director_summary": dirsum.get("ok"),
                            "demand_pdfs": (publish_demand_pdfs().get("pushed") if snap.get("ok") else None),
                            "receipt_pdfs": (publish_receipt_pdfs().get("pushed") if snap.get("ok") else None)}
    return out


def pull_doc_requests() -> dict[str, Any]:
    """Pull director 'generate document' requests queued on WordPress, generate each (a receipt pack or a
    demand PDF), and acknowledge them. WordPress cannot reach Mongoose (CGNAT), so it only queues; the
    generated document rides back out on the next snapshot. generate_receipt_pack is idempotent."""
    res = _wp_call("GET", _profile.wp_route("/sync/doc-requests"))
    if not res.get("ok"):
        _record_sync_run("pull_doc_requests", False, res.get("error") or "pull failed")
        return {"ok": False, "error": res.get("error"), "generated": 0}
    data = res.get("data") or {}
    reqs = data.get("requests") or []
    if not isinstance(reqs, list):
        reqs = []
    generated, req_ids = 0, []
    for q in reqs:
        if not isinstance(q, dict):
            continue
        dtype = str(q.get("doc_type") or "receipt")
        fid = str(q.get("flat_id") or "")
        doc = str(q.get("document_number") or "")
        try:
            if dtype == "all":
                # Backfill everything the portal is missing in one request.
                rr = ensure_missing_receipts()
                miss = missing_documents().get("missing_demand_pdfs", [])
                pdfn = sum(1 for m in miss if regenerate_demand_pdf(document_number=m["document_number"]).get("ok"))
                generated += rr.get("count", 0) + pdfn
                r = {"ok": True}
            elif dtype == "demand_pdf" and doc:
                r = regenerate_demand_pdf(document_number=doc)
                if r.get("ok"):
                    generated += 1
            elif dtype == "demand_mailing":
                # A director queued the batch payment-now-due mailing from WordPress admin (an
                # authenticated rtm_manage_portal action, which is the explicit confirm). Mongoose runs
                # it with all its rails: per-recipient individual send, PDF amount/ref cross-check, and
                # idempotency (a demand already emailed this period is skipped), so a re-drained or
                # timer-raced request never double-sends. Per-recipient failures are recorded in the
                # send-log and retried only if re-queued.
                from . import rtm_demand_mailing as _DM
                period = str(q.get("period") or "").strip() or None
                batch = _DM.send_batch(period, confirm=True, force=bool(q.get("force")))
                generated += int(batch.get("sent", 0)) if isinstance(batch, dict) else 0
                r = {"ok": True, "sent": batch.get("sent"), "failed": batch.get("failed")} if isinstance(batch, dict) else {"ok": False}
            elif dtype == "send_receipt" and (fid or doc):
                # Email a leaseholder their receipt + branded cover. Sensitive (real email), so gated.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD send_receipt: portal writes untrusted")
                    r = {"ok": False}
                else:
                    r = send_receipt_email(fid, "")
                    if r.get("ok"):
                        generated += 1
            elif dtype == "unmark_paid" and (fid or doc):
                # Reverse a recorded payment (clean reset). Sensitive money-ledger write, so gated.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD unmark_paid: portal writes untrusted")
                    r = {"ok": False}
                else:
                    from . import rtm_management as _M
                    rev = _M.reverse_payment(flat=fid, document_number=doc)
                    r = {"ok": bool(rev.get("ok"))}
                    if rev.get("ok"):
                        generated += 1
            elif dtype == "agree_payment_plan" and fid:
                # Agree an arrears instalment plan for the flat. Records the plan + emails the branded
                # payment-plan letter (via the same SMTP + verification path as receipts). Gated (writes).
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD agree_payment_plan: portal writes untrusted")
                    r = {"ok": False}
                else:
                    from .rtm_payment_plans import agree_payment_plan
                    months = int(q.get("months") or 6)
                    plan = agree_payment_plan(flat=fid, months=months)
                    # DO NOT auto-email the leaseholder. A WordPress button click used to record the
                    # plan AND immediately send the letter, so a wrong schedule (or a wrong greeting)
                    # reached a real leaseholder with nobody having seen it. The plan is recorded here;
                    # the letter is a separate, explicitly confirmed step by the owner. Financial
                    # correspondence to a third party must never be a side effect of one click.
                    r = {"ok": bool(plan.get("ok")), "letter": "awaiting_owner_confirmation"}
                    if plan.get("ok"):
                        generated += 1
            elif dtype == "send_plan_letter" and fid:
                # Email the payment-plan letter, asked for from WordPress. Composed HERE, never there: the
                # letter states what was agreed and what has actually been paid, and those figures must
                # come from the ledger rather than from a snapshot WordPress happens to be holding.
                # Sensitive (real correspondence to a leaseholder), so gated like every other write.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD send_plan_letter: portal writes untrusted")
                    r = {"ok": False}
                else:
                    # confirm=True is carried by the WordPress click itself: an authenticated
                    # rtm_manage_portal action behind a dialog that names the recipient and the position.
                    res = send_payment_plan_letter(fid, confirm=True)
                    r = {"ok": bool(res.get("ok"))}
                    if r["ok"]:
                        generated += 1
            elif dtype == "map_expenditure":
                # A director assigned a bank expenditure line to a budget category from the plugin. The
                # payload rides in document_number as "bank_txn_id|schedule|budget_line". Mongoose creates a
                # Paid payable under that category (so budget-vs-actual reflects it) and learns the payee.
                # Sensitive money-ledger write, so gated like the rest.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD map_expenditure: portal writes untrusted")
                    r = {"ok": False}
                else:
                    from . import rtm_management as _M
                    parts = str(doc or "").split("|")
                    if len(parts) >= 3 and parts[0].strip():
                        res = _M.map_expenditure(parts[0].strip(),
                                                 (parts[1].strip() or "sch1"), "|".join(parts[2:]).strip())
                        r = {"ok": bool(res.get("ok"))}
                        if r["ok"]:
                            generated += 1
                    else:
                        r = {"ok": False}
            elif dtype == "record_on_account" and fid:
                # A director recorded a payment ON ACCOUNT from WordPress (an advance for a budget not yet
                # demanded, or an overpayment). The AMOUNT rides in document_number. Mongoose books it as
                # standing credit (no refunds) and sweeps it onto any open demand; the excess carries
                # forward. Sensitive money write, so gated like the rest.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD record_on_account: portal writes untrusted")
                    r = {"ok": False}
                else:
                    from . import rtm_management as _M
                    amt = str(doc or "").strip()
                    res = _M.record_on_account_credit(flat=fid, amount=amt, note="via portal") if amt else {"ok": False}
                    r = {"ok": bool(res.get("ok"))}
                    if r["ok"]:
                        generated += 1
            elif dtype == "record_plan_instalment" and fid:
                # Record one plan instalment, from the plugin's one-click button. The AMOUNT IS NOT TAKEN
                # FROM WORDPRESS: it is looked up from the agreed schedule here, so a tampered form field
                # cannot post a wrong sum against a leaseholder's account. The instalment number IS honoured
                # because it makes the request idempotent: without it a repeat would record the NEXT
                # instalment and take two payments for one click.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD record_plan_instalment: portal writes untrusted")
                    r = {"ok": False}
                else:
                    from .rtm_payment_plans import record_instalment
                    try:
                        n = int(str(doc).strip() or 0) or None
                    except Exception:
                        n = None
                    res = record_instalment(fid, n=n)
                    r = {"ok": bool(res.get("ok"))}
                    if res.get("ok"):
                        generated += 1
            elif dtype == "rebuild_documents":
                # Owner-triggered rebuild from WordPress. force=True re-renders everything rather than
                # only what changed, because the reason someone presses this is that they believe a
                # document is wrong and want it made again from scratch.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD rebuild_documents: writes untrusted")
                    r = {"ok": False}
                else:
                    from .rtm_overdue import refresh_documents_if_stale
                    res = refresh_documents_if_stale(force=True)
                    r = {"ok": bool(res.get("ok"))}
                    if r["ok"]:
                        generated += 1
            elif dtype == "postal_run":
                # Build a postal run from WordPress. WP cannot render PDFs and cannot pull from Mongoose
                # (CGNAT), so it queues a SELECTION and Mongoose produces the merged PDF + address CSV and
                # pushes both back. Re-resolution happens HERE, at build time, not when the basket was
                # assembled: a chaser must not go to someone who paid at lunchtime.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD postal_run: portal writes untrusted")
                    r = {"ok": False}
                else:
                    from .rtm_overdue import postal_run
                    import base64 as _b64, hashlib as _hash
                    from pathlib import Path as _P
                    # document_number carries "kind:flat,flat". It used to arrive split across a `period`
                    # column that does not exist on the requests table, so nothing ever reached here.
                    raw = str(doc or "")
                    kind, _, flatstr = raw.partition(":")
                    kind = (kind or "chaser").strip().lower()
                    # An optional "|PERIOD" suffix scopes a demand run to one budget (e.g. kind:|2027-H1
                    # posts the 2027-H1 demands; without it the newest demand per flat is used).
                    flatstr, _, period_sel = flatstr.partition("|")
                    period_sel = period_sel.strip()
                    picks = [x.strip().upper() for x in flatstr.split(",") if x.strip()]
                    res = postal_run(kind, picks or None, dry_run=False, period=period_sel or "")
                    r = {"ok": bool(res.get("ok"))}
                    if res.get("ok"):
                        # Push both artefacts under the run id so the plugin can offer them as downloads.
                        # Carry the letter count and pages-per-letter in the document_number so the
                        # plugin can show Docmail's upload settings (pages-per-document = P) without
                        # re-reading the PDF. Both artefacts carry the same L/P.
                        _lp = f"~L{int(res.get('letters') or 0)}~P{int(res.get('pages_per_letter') or 0)}"
                        for label, path in (("pdf", res["pdf"]), ("csv", res["csv"])):
                            raw = _P(path).read_bytes()
                            sha = _hash.sha256(raw).hexdigest()
                            _wp_call("POST", _profile.wp_route("/sync/demand-pdf"), timeout=90.0,
                                     json_body={"pdf_key": sha[:43],
                                                "document_number": f"RUN/{res['run_id']}{_lp}#{label}",
                                                "flat_id": fid or "RUN",
                                                "content_b64": _b64.b64encode(raw).decode("ascii"),
                                                "sha256": sha})
                        generated += 1
            elif dtype == "resend_demand" and fid:
                # Re-send the payment-due cover with reminder wording plus the attached demand. Routed
                # through send_batch so it inherits every guard the batch has: the attachment is re-verified
                # against the demand, and a flat on an on-track payment plan is excluded, so this cannot
                # chase someone who is paying by agreement even if the button was clicked on the wrong row.
                if not _portal_writes_trusted():
                    _record_sync_run("pull_doc_requests", True, "HELD resend_demand: portal writes untrusted")
                    r = {"ok": False}
                else:
                    from .rtm_demand_mailing import send_batch
                    res = send_batch(flats=[fid], reminder=True, confirm=True, force=True)
                    r = {"ok": int(res.get("sent") or 0) > 0}
                    if r["ok"]:
                        generated += 1
            elif dtype == "export":
                # A director queued an accountant export from WordPress. Build the multiformat pack
                # (accounting CSVs, QIF/OFX, XLSX, branded PDF, JSON) plus the evidence bundle, file it into
                # Docs (doc_type=export) as a durable record, and mint a 24h tunnel download link the
                # director's browser fetches directly. The pack itself is NOT pushed into WordPress (it can
                # be several MB); only the small ready-metadata (link + summary) rides to the plugin.
                from . import rtm_export as _X
                from . import rtm_templates as _T
                # The queue only carries document_number, so the plugin packs the request as
                # "fmt:xero,pdf,...;ev:1" (like map_expenditure packs txn|sched|line). Parse it.
                fmts, ev = None, True
                for part in str(doc or "").split(";"):
                    part = part.strip()
                    if part.startswith("fmt:"):
                        fmts = [f.strip() for f in part[4:].split(",") if f.strip()] or None
                    elif part.startswith("ev:"):
                        ev = part[3:].strip() not in ("0", "false", "no", "")
                exp = _X.build_export(year="2027", formats=fmts, include_evidence=ev)
                if exp.get("ok") and exp.get("path"):
                    try:
                        from . import rtm_docs as _D
                        _D.ingest(content=Path(exp["path"]).read_bytes(), filename=exp["filename"],
                                  doc_type="export", uploaded_by="director", role="director",
                                  source="wp_export", note="Accountant export pack")
                    except Exception:
                        pass
                    token = _T.mint_download(exp["path"], exp["filename"], ttl_s=86400,
                                             media_type="application/zip")
                    try:
                        from .remote_access import public_base_url as _pbu
                        base = _pbu()
                    except Exception:
                        base = None
                    url = (base.rstrip("/") + "/rtm/doc/" + token) if base else ("/rtm/doc/" + token)
                    man = exp.get("manifest", {})
                    _wp_call("POST", _profile.wp_route("/sync/export-ready"), json_body={
                        "url": url, "filename": exp["filename"], "size": exp.get("size", 0),
                        "formats": man.get("formats", []), "evidence_files": man.get("evidence_files", 0),
                        "built_at": man.get("generated_at", "")})
                    generated += 1
                    r = {"ok": True}
                else:
                    r = {"ok": False}
            else:
                r = generate_receipt_pack(flat_id=fid, document_number=doc)
                if r.get("ok"):
                    generated += 1
        except Exception as e:
            _record_sync_run("pull_doc_requests", False, f"req {q.get('request_id')}: {e}")
            continue  # leave PENDING so the next pull retries , never ack a document we did not build
        # ACK ON SUCCESS ONLY: a request that did not generate stays pending and is retried, so WordPress
        # never marks a document "done" that Mongoose never produced.
        if r.get("ok") and q.get("request_id") is not None:
            req_ids.append(q.get("request_id"))
    if req_ids:
        _wp_call("POST", _profile.wp_route("/sync/doc-requests-ack"), json_body={"request_ids": req_ids})
    if generated:
        # Push the changed state (new receipt PDFs, or a reversed demand back to Issued) synchronously, so
        # the portal reflects it on the next refresh rather than waiting for the periodic guard.
        try:
            publish_receipt_pdfs()
        except Exception:
            pass
        try:
            publish_portal_snapshot(full=True)
        except Exception:
            pass
    _record_sync_run("pull_doc_requests", True, f"pulled={len(reqs)} generated={generated}")
    return {"ok": True, "pulled": len(reqs), "generated": generated}


def missing_portal_assets() -> dict[str, Any]:
    """List demands that are portal-ready but missing their PDF."""
    _ensure_portal_tables()
    from ..db import connect
    with connect() as con:
        rows = con.execute(
            """
            SELECT d.document_number, d.flat_id, d.period FROM rtm_mgmt_demands d
            LEFT JOIN portal_demand_pdfs p ON p.document_number = d.document_number
            WHERE p.document_number IS NULL
            ORDER BY d.document_number
            """).fetchall()
    return {"ok": True, "missing": [dict(r) for r in rows]}


# ── Brain tool entry-point ────────────────────────────────────────────────────

def portal_brain_summary() -> dict[str, Any]:
    """Summary for the brain's rtm_portal tool. Fast; skips the live WP probe."""
    readiness = portal_readiness(probe_wp=False)
    env = _portal_env()
    return {
        "portal_status": "implemented",
        "version": PORTAL_VERSION,
        "portal_ready": readiness["ok"],
        "bank_details_configured": _bank_details_ok(env),
        "wp_configured": _wp_configured(env),
        "readiness_errors": readiness["errors"],
        "readiness_warnings": readiness["warnings"],
        "caveat_7day": CAVEAT_7DAY,
        "next_actions": [
            c["detail"] for c in readiness["checks"]
            if not c.get("ok") and c.get("severity") in ("error", "warning")
        ][:5],
    }
