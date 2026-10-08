"""Batch payment-now-due mailing for Example Court service charge demands.

One command turns "everyone with an outstanding demand and an email on file" into a per-flat
mailing: the flat's attached demand PDF plus a courteous "payment now due" cover email that
repeats the amount, bank block and payment reference so nobody has to open the attachment to pay.

Design invariants (BATCH_MAILING_SPEC.md 7):
  1. No em/en dash in any generated cover email or subject (enforced by test).
  2. One recipient per message, one attachment, never BCC (differing attachments must never cross).
  3. The attached PDF's amount + reference must match the recipient's demand, or the send is refused.
  4. Excluded (paid / test / no-email) flats can never be recipients.
  5. Dry-run is the default; a live send needs an explicit confirm (SENSITIVE_TOOLS gate).
  6. Record on success only; a failed send stays visible and retryable, never silently "sent".
  7. The system narrates and sends; it moves no money.

This module owns cohort resolution + cover rendering + the dry-run preview. The actual send goes
through the WordPress channel (send_demand_email -> WP /portal/send-demand), mirroring
rtm_portal.send_receipt_email, because Mongoose's own mail connectors are read-only by design.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .. import profile as _profile
from . import rtm_management as MGMT
from . import rtm_templates as TPL

# Statuses that mean "no longer owed": these flats are never chased.
_SETTLED_STATUSES = {"paid", "closed", "cancelled", "void", "superseded"}

_MONTHS = ["", "January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]

_DASH_RE = re.compile("[\u2012\u2013\u2014\u2015\u2212]")  # figure/en/em/bar/minus (escaped: file stays dash-free)


def _gbp(pence: int) -> str:
    """Money with a thousands separator, matching the posted pack (e.g. 107516 -> '£1,075.16')."""
    return f"£{pence / 100:,.2f}"


def _human_date(iso: str) -> str:
    """'2026-07-30' -> '30 July 2026'. Falls back to the raw string if it is not an ISO date."""
    try:
        y, m, d = (int(x) for x in str(iso).split("-")[:3])
        return f"{d} {_MONTHS[m]} {y}"
    except Exception:
        return str(iso or "")


def _period_end_label(period: str) -> str:
    """'2026-H2' -> '31 December 2026'; '2026-H1' -> '30 June 2026'. Falls back to the period itself."""
    p = str(period or "").strip().upper()
    m = re.match(r"^(\d{4})-H([12])$", p)
    if not m:
        return p
    year = m.group(1)
    return f"30 June {year}" if m.group(2) == "1" else f"31 December {year}"


def _road_for(block: str) -> str:
    """Block code -> road from the config-driven block map (no postcode, as the cover subject uses)."""
    road, _ = TPL._BLOCKS.get((block or "").strip().upper()[:3], ("", ""))
    return road


def _current_period() -> str:
    """The period to mail: the most common period among demands that still have an outstanding balance,
    so a caller does not have to name it. Falls back to the newest demand's period, else ''."""
    demands = MGMT.list_demands(limit=5000)
    open_periods = [d.get("period") for d in demands if int(d.get("outstanding_pence", 0)) > 0]
    if open_periods:
        # most common outstanding period
        return max(set(open_periods), key=open_periods.count)
    return demands[0].get("period", "") if demands else ""


def _latest_open_demand_for_flat(flat_id: str, period: str) -> dict[str, Any] | None:
    """The single current demand to mail for a flat in a period: highest revision, outstanding > 0,
    not a settled status. list_demands already computes outstanding from allocations."""
    rows = [d for d in MGMT.list_demands(flat=flat_id, limit=5000)
            if str(d.get("period")) == period]
    if not rows:
        return None
    rows.sort(key=lambda d: int(d.get("revision", 0)), reverse=True)
    top = rows[0]
    if str(top.get("status", "")).strip().lower() in _SETTLED_STATUSES:
        return None
    if int(top.get("outstanding_pence", 0)) <= 0:
        return None
    # Never email a payment-due demand to a flat that is in NET CREDIT (paid on account): the money is
    # already in hand, and a demand email would ask for it again. Belt-and-braces over the outstanding
    # check, for standing credit not yet applied to this period's demand.
    try:
        if MGMT.flat_statement(flat_id).get("position") == "credit":
            return None
    except Exception:
        pass
    return top


def _as_reminder(subject: str, body: str, recipient: dict[str, Any]) -> tuple[str, str]:
    """Turn the first-issue cover into a reminder.

    Sending the original text again would be wrong twice over. It says the demand "is attached and is now
    due for payment" and asks whether the recipient received it, which reads absurdly on a second sending
    two weeks later, and it gives no reason for the email arriving again. It also states "payable by <date>"
    when the honest thing on the due date is to say the payment is due today.

    Only the opening is replaced. The bank block, payment reference, contact line and signature are the
    parts that must not drift from the attached demand, so they are left exactly as rendered.
    """
    from datetime import date as _date
    due = str(recipient.get("due_date") or "")
    is_today = due[:10] == _date.today().isoformat()
    when = "today" if is_today else f"on {_human_date(due)}"
    amount = recipient.get("amount") or _gbp(int(recipient.get("outstanding_pence") or 0))
    ref = recipient.get("payment_reference") or ""
    # VERBATIM. greeting_name is the salutation the director chose per flat ("Dr Morgan", "Alex and
    # Sam Barnes"). Taking the first token turned that into "Dear Dr" and "Dear Alex", overriding
    # a deliberate choice; on a titled holder it is also simply wrong.
    who = (str(recipient.get("greeting_name") or "").strip()
           or TPL.first_name_from(str(recipient.get("name") or "")))

    lines = body.splitlines()
    # Keep the contact line and signature only. Splicing from the bank-details line instead kept the
    # first-issue paragraph that follows it ("The demand was also posted to you... let us know if you've
    # not received it"), which repeated the crossed-payment sentence and still read as a first issue.
    keep_from = next((i for i, ln in enumerate(lines) if ln.lower().startswith("any questions")), None)
    tail = "\n".join(lines[keep_from:]).strip() if keep_from is not None else ""

    opening = (
        f"Dear {who}," if who else "Hello,",
        "",
        f"A reminder that the service charge for your flat is due for payment {when}.",
        "",
        (f"The amount outstanding is {amount}, payable by bank transfer to "
         f"{TPL.BRANDING['company']}, {TPL._BANK['name']}, sort code {TPL._BANK['sort_code']}, "
         f"account {TPL._BANK['account_number']}, using payment reference {ref} so the payment can be "
         f"allocated correctly. The demand is attached again for convenience."),
        "",
        "If your payment has already crossed with this email, thank you, and a receipt will follow shortly.",
        "",
        "",
    )
    new_subject = f"Reminder. Service charge for {recipient.get('unit', '')}, payment due {when}".strip()
    return _DASH_RE.sub(" ", new_subject), _DASH_RE.sub(" ", "\n".join(opening) + tail)


def resolve_cohort(period: str | None = None, *, flats: "list[str] | None" = None) -> dict[str, Any]:
    """Who gets a payment-now-due email, and who is excluded and why.

    A recipient is a real (excluded=0) flat with an email on file whose current demand for the period
    still has an outstanding balance. Everyone else is returned in `excluded` with a reason, so the
    exclusions are surfaced, never silent. Deterministic order (block, flat_number).

    A flat with an ON-TRACK payment plan is EXCLUDED. It still has an outstanding balance, so it used to
    qualify, but chasing someone for immediate payment of a sum they are already paying by an agreement the
    directors made with them is wrong on the facts and contradicts the letter they were sent. That is the
    same class of error as the payment-plan letter that went out incorrectly. A plan holder who has MISSED
    an instalment is not protected: the plan is what excuses the balance, and breaking it removes the excuse.

    `flats` restricts the cohort to named flats (for a targeted resend), applied without weakening any of
    the exclusions above: naming a flat asks for it to be considered, not for the safety checks to be skipped.
    """
    period = period or _current_period()
    only = {str(f).strip().upper().replace("-", "").replace(" ", "") for f in (flats or [])} or None
    recipients: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for fl in MGMT.list_flats(include_excluded=True):
        flat_id = str(fl.get("flat_id"))
        block = str(fl.get("block") or flat_id[:3])
        num = fl.get("flat_number")
        name = str(fl.get("leaseholder_name") or "")
        email = str(fl.get("leaseholder_email") or "").strip()
        unit = f"{block} F{num}"

        if only is not None and flat_id not in only:
            continue                      # not asked for; not an exclusion worth reporting

        if int(fl.get("excluded", 0) or 0) == 1:
            excluded.append({"flat_id": flat_id, "unit": unit, "name": name, "reason": "test/excluded flat"})
            continue

        try:
            from . import rtm_payment_plans as _PP
            _prog = _PP.plan_progress(flat_id)
        except Exception:
            _prog = None
        if _prog and _prog.get("on_track"):
            excluded.append({"flat_id": flat_id, "unit": unit, "name": name,
                             "reason": (f"payment plan agreed and on track "
                                        f"({_prog['instalments_paid']} of {_prog['months']} paid, "
                                        f"{_prog['outstanding']} remaining)")})
            continue

        demand = _latest_open_demand_for_flat(flat_id, period)
        if demand is None:
            excluded.append({"flat_id": flat_id, "unit": unit, "name": name,
                             "reason": "no outstanding demand (paid or none)"})
            continue

        if not email:
            excluded.append({"flat_id": flat_id, "unit": unit, "name": name,
                             "reason": "no email on file (post/door-drop only)",
                             "amount": _gbp(int(demand["outstanding_pence"]))})
            continue

        pence = int(demand["amount_pence"])
        recipients.append({
            "flat_id": flat_id,
            "unit": unit,
            "block": block,
            "flat_number": num,
            "name": name,
            "greeting_name": str(fl.get("greeting_name") or "").strip() or TPL.first_name_from(name) or name,
            "email": email.lower(),
            "amount_pence": pence,
            "amount": _gbp(pence),
            "outstanding_pence": int(demand["outstanding_pence"]),
            "payment_reference": str(fl.get("payment_reference") or "").strip() or _fallback_ref(name),
            "document_number": str(demand["document_number"]),
            "due_date": str(demand.get("due_date") or ""),
            "period": period,
            "road": _road_for(block),
            "is_member": _is_member(flat_id),
            # Resolved here too. Three separate reads of this column existed, which is how the
            # subsystems drifted: each one could be fixed while another stayed wrong.
            "cc_list": MGMT.correspondence_cc(flat_id),
        })

    recipients.sort(key=lambda r: (r["block"], int(r["flat_number"] or 0)))
    excluded.sort(key=lambda r: r["flat_id"])
    return {"period": period, "period_end": _period_end_label(period),
            "recipients": recipients, "excluded": excluded,
            "count": len(recipients), "excluded_count": len(excluded)}


def _fallback_ref(name: str) -> str:
    """Surname upper-cased, punctuation stripped (O'Brien -> OBRIEN), matching the demand generator."""
    surname = (name or "").replace(" and ", " ").split()[-1] if name.strip() else ""
    return re.sub(r"[^A-Z0-9]", "", surname.upper())


def _is_member(flat_id: str) -> bool:
    try:
        from . import rtm_members
        return flat_id.upper() in {f.upper() for f in rtm_members.member_flat_ids()}
    except Exception:
        return False


def render_cover(recipient: dict[str, Any], period_end: str) -> dict[str, Any]:
    """Render the dash-free payment-now-due cover email for one recipient.

    Bank details come from the single canonical BRANDING block (which matches the demand PDF), so the
    cover email can never drift from the attached document. Returns render_email's {ok, text,
    ready_to_send, missing}, with a hard dash-check appended (dashes fail ready_to_send).
    """
    ctx = {
        "recipient_email": recipient["email"],
        "greeting_name": recipient["greeting_name"],
        "flat_number": recipient["flat_number"],
        "road": recipient["road"],
        "period_end": period_end,
        "amount_due": recipient["amount"],
        "due_date": _human_date(recipient["due_date"]),
        "payment_ref": recipient["payment_reference"],
        "bank_name": TPL.BRANDING["bank_name"],
        "bank_sort_code": TPL.BRANDING["bank_sort_code"],
        "bank_account_number": TPL.BRANDING["bank_account_number"],
        "bank_account_name": TPL.BRANDING["bank_account_name"],
    }
    out = TPL.render_email("cover_email_demand", ctx)
    if out.get("ok"):
        dashes = _DASH_RE.findall(out.get("text", ""))
        out["dash_free"] = not dashes
        if dashes:
            # Invariant 1: authored cover copy must never carry a dash. Refuse to mark it sendable.
            out["ready_to_send"] = False
            out["dash_count"] = len(dashes)
    return out


def demand_pdf_for(document_number: str) -> tuple[str, str] | None:
    """Resolve a demand's stored PDF to (path, sha256) from portal_demand_pdfs, the canonical demand
    store (NOT the evidence store rtm_trust._find_demand_pdf reads, which holds only intake evidence).
    Returns None if there is no stored PDF for this demand."""
    try:
        from ..db import connect
        with connect() as con:
            row = con.execute(
                "SELECT pdf_storage_path, pdf_sha256 FROM portal_demand_pdfs WHERE document_number=?",
                (document_number,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    path = str(row["pdf_storage_path"] or "")
    import os as _os
    if not (path and _os.path.exists(path)):
        return None
    return path, str(row["pdf_sha256"] or "")


def verify_attachment(recipient: dict[str, Any]) -> dict[str, Any]:
    """Invariant 3: the attached PDF must be the recipient's real demand, carrying their amount AND
    payment reference, or the send is refused. Reads the PDF text and asserts both appear. Never
    raises: a missing library or unreadable PDF returns ok=False with a reason."""
    resolved = demand_pdf_for(recipient["document_number"])
    if not resolved:
        return {"ok": False, "reason": "no stored demand PDF", "path": None}
    path, sha256 = resolved
    try:
        try:
            from pypdf import PdfReader
        except Exception:
            from PyPDF2 import PdfReader  # type: ignore
        text = "".join(pg.extract_text() or "" for pg in PdfReader(path).pages)
    except Exception as e:
        return {"ok": False, "reason": f"could not read PDF: {e}", "path": path}
    amount_ok = recipient["amount"] in text
    ref_ok = recipient["payment_reference"] in text
    if not (amount_ok and ref_ok):
        return {"ok": False, "path": path, "sha256": sha256,
                "reason": f"PDF cross-check failed (amount={amount_ok}, ref={ref_ok})"}
    return {"ok": True, "path": path, "sha256": sha256}


def _cover_parts(text: str) -> tuple[str, str]:
    """Split a rendered cover into (subject, body). The template's leading 'To:'/'Subject:' lines are
    metadata for the preview and the .eml download; the SENT email body is only the message beneath them
    (from the salutation onward), so those header lines never leak into the delivered email."""
    lines = (text or "").splitlines()
    subject = ""
    body_start = 0
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("subject:"):
            subject = ln.split(":", 1)[1].strip()
        if ln.strip() == "":
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip("\n")
    return subject, (body or text)


def prepare(period: str | None = None, *, flats: "list[str] | None" = None,
            reminder: bool = False) -> dict[str, Any]:
    """Dry-run: resolve the cohort and render every cover email, sending nothing.

    Returns the full preview: per-recipient subject/body/amount/reference/document_number plus the
    exclusions with reasons. This is what the WordPress "Prepare payment-due mailing" action and the
    cockpit card show the director before anything is sent.
    """
    cohort = resolve_cohort(period, flats=flats)
    period_end = cohort["period_end"]
    bundles: list[dict[str, Any]] = []
    problems: list[str] = []
    for r in cohort["recipients"]:
        cover = render_cover(r, period_end)
        subject, body = _cover_parts(cover.get("text", ""))
        if reminder:
            subject, body = _as_reminder(subject, body, r)
        att = verify_attachment(r)
        if not cover.get("ready_to_send"):
            problems.append(f"{r['flat_id']}: cover not sendable "
                            f"(missing={cover.get('missing')}, dash_free={cover.get('dash_free')})")
        if not att.get("ok"):
            problems.append(f"{r['flat_id']}: attachment refused ({att.get('reason')})")
        ready = bool(cover.get("ready_to_send")) and bool(att.get("ok"))
        bundles.append({
            "flat_id": r["flat_id"], "unit": r["unit"], "to": r["email"], "to_name": r.get("name", ""),
            # via the shared resolver, so demands, receipts and letters copy an identical set
            "cc": ", ".join(MGMT.correspondence_cc(r["flat_id"])),
            "subject": subject, "body": body,
            "amount": r["amount"], "payment_reference": r["payment_reference"],
            "document_number": r["document_number"], "is_member": r["is_member"],
            "attachment_ok": bool(att.get("ok")), "attachment_reason": att.get("reason", ""),
            "attachment_path": att.get("path"), "attachment_sha256": att.get("sha256", ""),
            "ready": ready, "dash_free": bool(cover.get("dash_free", True)),
        })
    return {
        "period": cohort["period"], "period_end": period_end,
        "recipient_count": cohort["count"], "excluded_count": cohort["excluded_count"],
        "bundles": bundles, "excluded": cohort["excluded"], "problems": problems,
        "dry_run": True, "generated_on": date.today().isoformat(),
    }


# --------------------------------------------------------------------------- sent-log (idempotency)

def _ensure_sends_table() -> None:
    from ..db import connect
    with connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS rtm_demand_sends(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flat_id TEXT NOT NULL,
                document_number TEXT NOT NULL,
                period TEXT NOT NULL,
                to_email TEXT NOT NULL,
                cc TEXT NOT NULL DEFAULT '',
                subject TEXT,
                message_id TEXT,
                status TEXT NOT NULL,          -- 'sent' | 'failed'
                error TEXT,
                sent_at TEXT NOT NULL
            )
            """
        )
        if "cc" not in {r[1] for r in con.execute("PRAGMA table_info(rtm_demand_sends)").fetchall()}:
            con.execute("ALTER TABLE rtm_demand_sends ADD COLUMN cc TEXT NOT NULL DEFAULT ''")
        # One successful send per (flat, demand, period): the idempotency guard. Failures are not
        # unique-constrained (a flat can fail then succeed), so the partial index covers only 'sent'.
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_demand_sends_once "
            "ON rtm_demand_sends(flat_id, document_number, period) WHERE status='sent'"
        )
        con.commit()


def already_sent(flat_id: str, document_number: str, period: str) -> bool:
    """True if this exact demand+period was already emailed successfully to this flat (invariant 6:
    never double-send)."""
    _ensure_sends_table()
    from ..db import connect
    with connect() as con:
        row = con.execute(
            "SELECT 1 FROM rtm_demand_sends WHERE flat_id=? AND document_number=? AND period=? "
            "AND status='sent' LIMIT 1", (flat_id, document_number, period)).fetchone()
    return row is not None


def _record_send(flat_id: str, document_number: str, period: str, to_email: str,
                 subject: str, ok: bool, message_id: str = "", error: str = "", cc: str = "") -> None:
    """Record a send outcome. Invariant 6: only a genuine success writes a 'sent' row; a failure is
    logged as 'failed' so it is visible and retryable, never silently counted as sent.

    A RESEND must be recordable. UNIQUE(flat_id, document_number, period) made a second send of the same
    demand impossible to log, so a legitimate reminder raised IntegrityError. Because the recorder runs
    AFTER the transport, and the exception was uncaught, the first recipient's email went out unlogged and
    the batch aborted before the second recipient was attempted at all: one leaseholder chased, one not,
    and no record of either. force=True bypassed the already_sent SKIP but nothing bypassed this INSERT.

    So a repeat now appends an attempt instead of colliding, and the original send is preserved. That is
    also the more correct audit: the log must be able to show that a leaseholder was chased twice.
    """
    _ensure_sends_table()
    from ..db import connect, utc_now_iso
    with connect() as con:
        prior = con.execute(
            "SELECT COUNT(*) FROM rtm_demand_sends WHERE flat_id=? AND document_number=? AND period=?",
            (flat_id, document_number, period)).fetchone()[0]
        status = ("sent" if ok else "failed") if not prior else ("resent" if ok else "resend-failed")
        try:
            con.execute(
                "INSERT INTO rtm_demand_sends(flat_id,document_number,period,to_email,cc,subject,message_id,status,error,sent_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (flat_id, document_number, period, to_email, cc or "", subject, message_id,
                 status, error or "", utc_now_iso()))
        except Exception:
            # The legacy UNIQUE index is still on the live table. Losing the audit row must never be worse
            # than it already is: keep the original and append this attempt to it rather than raising and
            # aborting a batch mid-flight.
            con.execute(
                "UPDATE rtm_demand_sends SET status=?, error=?, sent_at=?, subject=? "
                "WHERE flat_id=? AND document_number=? AND period=?",
                (status, (error or "")[:400], utc_now_iso(), subject,
                 flat_id, document_number, period))
        con.commit()


def sent_log(period: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    _ensure_sends_table()
    from ..db import connect
    with connect() as con:
        if period:
            rows = con.execute("SELECT * FROM rtm_demand_sends WHERE period=? ORDER BY sent_at DESC LIMIT ?",
                               (period, limit)).fetchall()
        else:
            rows = con.execute("SELECT * FROM rtm_demand_sends ORDER BY sent_at DESC LIMIT ?",
                               (limit,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- transport + orchestrator

def _wp_transport(bundle: dict[str, Any], period: str) -> dict[str, Any]:
    """Default live transport: send one demand via the WordPress channel, mirroring
    rtm_portal.send_receipt_email. Mongoose renders the dash-free cover; WordPress attaches the
    flat-gated demand PDF and sends it verbatim from the RTM mailbox. Individual send only."""
    from . import rtm_portal
    # Ensure this demand's PDF is in the WordPress store, then resolve its pdf_key.
    try:
        rtm_portal.publish_demand_pdfs(limit=500)
    except Exception:
        pass
    pdf_key = ""
    try:
        from ..db import connect
        with connect() as con:
            row = con.execute("SELECT pdf_key FROM portal_demand_pdfs WHERE document_number=?",
                              (bundle["document_number"],)).fetchone()
            pdf_key = str(row["pdf_key"]) if row else ""
    except Exception:
        pdf_key = ""
    if not pdf_key:
        return {"ok": False, "error": "demand PDF not in portal store"}
    res = rtm_portal._wp_call(
        "POST", _profile.wp_route("portal/send-demand"),
        json_body={
            "flat_id": bundle["flat_id"], "document_number": bundle["document_number"],
            "pdf_key": pdf_key, "to_email": bundle["to"], "to_name": bundle.get("to_name", ""),
            "cc": bundle.get("cc", ""),
            "subject": bundle["subject"], "body": bundle["body"],
            "priority": bundle.get("priority", ""),
        }, timeout=60.0)
    return res


def send_test(flat_id: str, *, to_override: str | None = None, transport=None,
              priority: str = "normal") -> dict[str, Any]:
    """Send ONE demand mailing for a single flat, bypassing the outstanding filter, to smoke-test the
    full pipeline (cover render, PDF resolve + amount/ref cross-check, the WordPress send channel, and
    the audit log) on a safe target such as the owner's own flat, even when that flat owes nothing.

    Uses the exact same mechanisms as a real send: same cover template, same verify_attachment, same
    transport, same sent-log (recorded with a status of 'test-sent'/'test-failed' so a test is never
    mistaken for a real chase and never blocks a later genuine send). force is implicit (idempotency is
    not applied to tests). to_override lets you redirect the single email to a chosen safe address.
    """
    flat_id = (flat_id or "").strip().upper()
    fl = MGMT._get_flat(flat_id)
    if not fl:
        return {"ok": False, "error": f"unknown flat {flat_id}"}
    rows = MGMT.list_demands(flat=flat_id, limit=5000)
    if not rows:
        return {"ok": False, "error": f"no demand on record for {flat_id}"}
    rows.sort(key=lambda d: int(d.get("revision", 0)), reverse=True)
    demand = rows[0]
    block = str(fl.get("block") or flat_id[:3])
    name = str(fl.get("leaseholder_name") or "")
    pence = int(demand["amount_pence"])
    period = str(demand.get("period") or _current_period())
    recipient = {
        "flat_id": flat_id, "unit": f"{block} F{fl.get('flat_number')}", "block": block,
        "flat_number": fl.get("flat_number"), "name": name,
        "greeting_name": str(fl.get("greeting_name") or "").strip() or TPL.first_name_from(name) or name,
        "email": (to_override or str(fl.get("leaseholder_email") or "")).strip().lower(),
        "amount_pence": pence, "amount": _gbp(pence),
        "outstanding_pence": int(demand.get("outstanding_pence", pence)),
        "payment_reference": str(fl.get("payment_reference") or "").strip() or _fallback_ref(name),
        "document_number": str(demand["document_number"]), "due_date": str(demand.get("due_date") or ""),
        "period": period, "road": _road_for(block), "is_member": _is_member(flat_id),
    }
    if not recipient["email"]:
        return {"ok": False, "error": f"{flat_id} has no email on file and no to_override was given"}

    cover = render_cover(recipient, _period_end_label(period))
    if not cover.get("ready_to_send"):
        return {"ok": False, "error": "cover not sendable",
                "missing": cover.get("missing"), "dash_free": cover.get("dash_free")}
    att = verify_attachment(recipient)
    if not att.get("ok"):
        return {"ok": False, "error": f"attachment refused: {att.get('reason')}"}

    subject, body = _cover_parts(cover["text"])
    bundle = {"flat_id": flat_id, "to": recipient["email"], "to_name": recipient.get("name", ""),
              "cc": ", ".join(MGMT.correspondence_cc(flat_id)),
              "subject": subject, "body": body, "document_number": recipient["document_number"],
              "amount": recipient["amount"], "payment_reference": recipient["payment_reference"],
              "priority": "high" if str(priority).lower() == "high" else ""}
    send = transport or _wp_transport
    try:
        r = send(bundle, period)
    except Exception as e:
        r = {"ok": False, "error": f"transport raised: {type(e).__name__}: {e}"}
    ok = bool(r.get("ok"))
    _ensure_sends_table()
    from ..db import connect, utc_now_iso
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_demand_sends(flat_id,document_number,period,to_email,cc,subject,message_id,status,error,sent_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (flat_id, recipient["document_number"], period, recipient["email"], bundle.get("cc", ""), subject,
             str(r.get("message_id", "")), "test-sent" if ok else "test-failed",
             str(r.get("error", "")), utc_now_iso()))
        con.commit()
    return {"ok": ok, "test": True, "flat_id": flat_id, "to": recipient["email"],
            "subject": subject, "amount": recipient["amount"], "reference": recipient["payment_reference"],
            "document_number": recipient["document_number"], "wp_response": r}


def send_batch(period: str | None = None, *, confirm: bool = False, force: bool = False,
               flats: "list[str] | None" = None, reminder: bool = False,
               transport=None, priority: str = "normal", throttle_seconds: float = 3.0) -> dict[str, Any]:
    """Prepare the mailing and, only when confirm=True, send it one recipient at a time.

    Invariants: dry-run by default (confirm=False returns the preview, sends nothing); one message per
    recipient (never BCC); a recipient whose cover or attachment is not ready is skipped, not sent; an
    already-sent demand+period is skipped unless force=True; every send is recorded on success only.

    `transport(bundle, period) -> {ok, message_id?, error?}` is injectable so tests exercise the send
    path against a safe capturing sink instead of the live WordPress channel (never the real
    leaseholders). Defaults to the WordPress transport.
    """
    prep = prepare(period, flats=flats, reminder=reminder)
    period = prep["period"]
    if not confirm:
        return {**prep, "sent": 0, "skipped": 0, "failed": 0,
                "note": "dry-run: nothing sent. Re-run with confirm=True to send."}

    # Guard against the exact mistake that wrote 11 false 'sent' rows once: an INJECTED (mock) transport
    # run against the canonical brain is always a test that never actually emails, yet record-on-success
    # would mark demands delivered and poison the idempotency guard. Refuse it on the real brain; tests
    # inject transports only against a temp MONGOOSE_DB_PATH, which passes this check.
    if transport is not None:
        try:
            from ..settings import get_settings
            dbp = str(get_settings().db_path)
        except Exception:
            dbp = ""
        if dbp.endswith("/.mongoose/data/mongoose.sqlite3"):
            raise RuntimeError(
                "send_batch: refusing an injected transport against the canonical brain. A mock transport "
                "never emails but would record demands as 'sent'. Run this against a temp MONGOOSE_DB_PATH.")

    send = transport or _wp_transport
    results: list[dict[str, Any]] = []
    sent = skipped = failed = 0
    for b in prep["bundles"]:
        if not b["ready"]:
            skipped += 1
            results.append({"flat_id": b["flat_id"], "to": b["to"], "outcome": "skipped",
                            "reason": b.get("attachment_reason") or "cover not sendable"})
            continue
        if not force and already_sent(b["flat_id"], b["document_number"], period):
            skipped += 1
            results.append({"flat_id": b["flat_id"], "to": b["to"], "outcome": "skipped",
                            "reason": "already sent"})
            continue
        b["priority"] = "high" if str(priority).lower() == "high" else ""
        try:
            r = send(b, period)
        except Exception as e:
            r = {"ok": False, "error": f"transport raised: {type(e).__name__}: {e}"}
        ok = bool(r.get("ok"))
        # A BOOKKEEPING FAILURE MUST NOT ABORT THE BATCH. This raised IntegrityError on recipient 1 of 2,
        # and because the transport had already run, one leaseholder was chased and the other was never
        # attempted. The send is the outward-facing act; failing to log it is bad but must not decide who
        # gets served. The failure is attached to the result so it is visible rather than swallowed.
        try:
            _record_send(b["flat_id"], b["document_number"], period, b["to"], b["subject"],
                         ok, message_id=str(r.get("message_id", "")), error=str(r.get("error", "")),
                         cc=b.get("cc", ""))
        except Exception as _le:
            r = {**r, "log_error": f"{type(_le).__name__}: {_le}"}
        if ok:
            sent += 1
            results.append({"flat_id": b["flat_id"], "to": b["to"], "outcome": "sent",
                            "message_id": r.get("message_id", "")})
        else:
            failed += 1
            results.append({"flat_id": b["flat_id"], "to": b["to"], "outcome": "failed",
                            "reason": r.get("error", "")})
        # Pace the batch: the mail relay delivers a rapid burst promptly for the first few then queues
        # the rest for minutes (a soft throttle). A short gap between sends keeps the whole batch prompt.
        # Skipped only for injected-transport tests (which never hit a real relay).
        if throttle_seconds and transport is None:
            import time as _time
            _time.sleep(throttle_seconds)
    return {"period": period, "period_end": prep["period_end"], "dry_run": False,
            "recipient_count": prep["recipient_count"], "sent": sent, "skipped": skipped,
            "failed": failed, "results": results, "excluded": prep["excluded"]}
