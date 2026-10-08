from __future__ import annotations

import csv
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from .. import profile as _profile
from ..db import audit, connect, get_app_state, set_app_state, utc_now_iso

# The RTM management spine (the company and its blocks come from the site profile).
# This is the operational register/ledger layer described in the RTM system spec:
# demands, Metro Bank payment reconciliation, arrears/recovery schedule, outgoing
# payables, budget-vs-actuals, and evidence records. It is deliberately single-
# operator, local-only and append-first. It does not send demands/reminders or move
# money; it prepares registers, review queues, statements and packs for the director.

DEMAND_STATUSES = {"Issued", "Part-paid", "Paid", "Overdue", "In recovery", "Superseded", "Cancelled"}
BANK_MATCH_STATUSES = {"exact", "partial", "unmatched", "duplicate", "reviewed"}
PAYABLE_STATUSES = {"Scheduled", "Approved", "Paid", "Cancelled"}
SCHEDULES = {"sch1", "sch2", "combined", "reserve", "one_off", "company_admin", "unknown"}

ARREARS_TIMELINE = [
    (90, "debt_referral", "Eligible for debt referral / pre-action review"),
    (56, "final_notice", "Final notice before recovery"),
    (28, "second_reminder", "Second reminder"),
    (14, "first_reminder", "First reminder"),
    (1, "overdue_flag", "Account flagged overdue"),
]


def _ensure_tables() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS rtm_mgmt_flats (
                flat_id TEXT PRIMARY KEY,
                block TEXT NOT NULL,
                flat_number INTEGER NOT NULL,
                leaseholder_name TEXT NOT NULL DEFAULT '',
                correspondence_address TEXT NOT NULL DEFAULT '',
                leaseholder_email TEXT NOT NULL DEFAULT '',
                leaseholder_email_alt1 TEXT NOT NULL DEFAULT '',
                leaseholder_email_alt2 TEXT NOT NULL DEFAULT '',
                correspondence_address_alt1 TEXT NOT NULL DEFAULT '',
                greeting_name TEXT NOT NULL DEFAULT '',
                excluded INTEGER NOT NULL DEFAULT 0,
                demand_cc TEXT NOT NULL DEFAULT '',
                payer_aliases TEXT NOT NULL DEFAULT '',
                apportionment_band TEXT NOT NULL DEFAULT '',
                payment_reference TEXT NOT NULL DEFAULT '',
                document_scheme TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_flats_payment_ref ON rtm_mgmt_flats(payment_reference);

            CREATE TABLE IF NOT EXISTS rtm_mgmt_demands (
                document_number TEXT PRIMARY KEY,
                flat_id TEXT NOT NULL,
                leaseholder_name TEXT NOT NULL DEFAULT '',
                period TEXT NOT NULL,
                issue_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                amount_pence INTEGER NOT NULL,
                delivery_method TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                supersedes_document_number TEXT,
                schedule TEXT NOT NULL DEFAULT 'sch1',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(flat_id) REFERENCES rtm_mgmt_flats(flat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_demands_flat ON rtm_mgmt_demands(flat_id);
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_demands_status_due ON rtm_mgmt_demands(status, due_date);

            CREATE TABLE IF NOT EXISTS rtm_mgmt_bank_transactions (
                id TEXT PRIMARY KEY,
                bank_date TEXT NOT NULL,
                description TEXT NOT NULL,
                reference TEXT NOT NULL DEFAULT '',
                amount_pence INTEGER NOT NULL,
                direction TEXT NOT NULL,
                match_status TEXT NOT NULL,
                matched_flat_id TEXT,
                matched_document_number TEXT,
                allocated_pence INTEGER NOT NULL DEFAULT 0,
                duplicate_of TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT 'metro_csv',
                -- Owner explanation of this line. Declared HERE as well as in the migration above, because
                -- a column that exists only in a live-DB ALTER is a column a fresh install does not have.
                notes TEXT NOT NULL DEFAULT '',
                explained_by TEXT NOT NULL DEFAULT '',
                explained_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            DROP INDEX IF EXISTS idx_rtm_mgmt_bank_fingerprint;
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_bank_fingerprint_lookup ON rtm_mgmt_bank_transactions(bank_date, amount_pence, description, reference);
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_bank_match ON rtm_mgmt_bank_transactions(match_status);

            CREATE TABLE IF NOT EXISTS rtm_mgmt_allocations (
                id TEXT PRIMARY KEY,
                bank_transaction_id TEXT NOT NULL,
                document_number TEXT NOT NULL,
                flat_id TEXT NOT NULL,
                amount_pence INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(bank_transaction_id) REFERENCES rtm_mgmt_bank_transactions(id),
                FOREIGN KEY(document_number) REFERENCES rtm_mgmt_demands(document_number)
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_allocations_demand ON rtm_mgmt_allocations(document_number);
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_allocations_bank ON rtm_mgmt_allocations(bank_transaction_id);

            -- On-account credit APPLIED to a demand. Kept apart from rtm_mgmt_allocations (which ties a
            -- real bank credit to a demand) because on-account money is a standing ledger credit with no
            -- bank transaction of its own at apply time: the money already arrived and was posted as an
            -- `on_account` ledger credit. This row records ONLY that a slice of that standing credit now
            -- covers a specific demand, so allocated_to_demand() nets it and the demand reads Paid/Part-paid
            -- WITHOUT posting a second ledger credit (which would double-count the money). credit_entry_id
            -- links back to the ledger credit consumed, so a reversal can find and release it.
            CREATE TABLE IF NOT EXISTS rtm_mgmt_credit_applications (
                id TEXT PRIMARY KEY,
                document_number TEXT NOT NULL,
                flat_id TEXT NOT NULL,
                amount_pence INTEGER NOT NULL,
                credit_entry_id TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(document_number) REFERENCES rtm_mgmt_demands(document_number)
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_credit_appl_demand ON rtm_mgmt_credit_applications(document_number);
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_credit_appl_flat ON rtm_mgmt_credit_applications(flat_id);

            CREATE TABLE IF NOT EXISTS rtm_mgmt_ledger_entries (
                id TEXT PRIMARY KEY,
                flat_id TEXT,
                fund TEXT NOT NULL DEFAULT 'service_charge_trust',
                schedule TEXT NOT NULL DEFAULT 'sch1',
                entry_date TEXT NOT NULL,
                kind TEXT NOT NULL,
                description TEXT NOT NULL,
                debit_pence INTEGER NOT NULL DEFAULT 0,
                credit_pence INTEGER NOT NULL DEFAULT 0,
                document_number TEXT,
                bank_transaction_id TEXT,
                payable_id TEXT,
                correction_of TEXT,
                immutable_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_ledger_flat_date ON rtm_mgmt_ledger_entries(flat_id, entry_date, created_at);
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_ledger_doc ON rtm_mgmt_ledger_entries(document_number);

            CREATE TABLE IF NOT EXISTS rtm_mgmt_recovery_actions (
                id TEXT PRIMARY KEY,
                document_number TEXT NOT NULL,
                flat_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                scheduled_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Scheduled',
                generated_text TEXT NOT NULL DEFAULT '',
                release_date TEXT,
                release_method TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(document_number) REFERENCES rtm_mgmt_demands(document_number)
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_recovery_doc ON rtm_mgmt_recovery_actions(document_number);
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_recovery_status_date ON rtm_mgmt_recovery_actions(status, scheduled_date);

            CREATE TABLE IF NOT EXISTS rtm_mgmt_payables (
                id TEXT PRIMARY KEY,
                supplier TEXT NOT NULL,
                description TEXT NOT NULL,
                schedule TEXT NOT NULL,
                budget_line TEXT NOT NULL DEFAULT '',
                amount_pence INTEGER NOT NULL,
                vat_rate REAL NOT NULL DEFAULT 0,
                vat_treatment TEXT NOT NULL DEFAULT '',
                invoice_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                cadence TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                approved_at TEXT,
                paid_date TEXT,
                paid_amount_pence INTEGER,
                bank_reference TEXT,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_payables_due ON rtm_mgmt_payables(status, due_date);
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_payables_budget ON rtm_mgmt_payables(schedule, budget_line);

            CREATE TABLE IF NOT EXISTS rtm_mgmt_budget_lines (
                id TEXT PRIMARY KEY,
                year TEXT NOT NULL,
                schedule TEXT NOT NULL,
                budget_line TEXT NOT NULL,
                amount_pence INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(year, schedule, budget_line)
            );

            -- Learned payee -> budget-category rules. When an expenditure is mapped to a category (auto or
            -- by the director), the payee pattern is remembered so the same supplier auto-suggests next time.
            CREATE TABLE IF NOT EXISTS rtm_mgmt_expense_rules (
                id TEXT PRIMARY KEY,
                pattern TEXT NOT NULL,
                schedule TEXT NOT NULL,
                budget_line TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(pattern)
            );

            CREATE TABLE IF NOT EXISTS rtm_mgmt_evidence (
                id TEXT PRIMARY KEY,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                document_kind TEXT NOT NULL,
                title TEXT NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                sha256 TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_mgmt_evidence_target ON rtm_mgmt_evidence(target_type, target_id);

            CREATE TABLE IF NOT EXISTS rtm_payee_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payee_key TEXT NOT NULL,
                payee_name TEXT NOT NULL DEFAULT '',
                sort_code TEXT NOT NULL DEFAULT '',
                account_number TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'seen',
                confirmed_by TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                confirmed_at TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(payee_key, sort_code, account_number)
            );
            CREATE INDEX IF NOT EXISTS idx_rtm_payee_bank_key ON rtm_payee_bank(payee_key);
            """
        )
        # Idempotent column top-ups. These columns were added to the live DB by hand in earlier work
        # but were never in the CREATE TABLE, so a fresh install lacked them: greeting_name (used to
        # address demand documents) and leaseholder_email (drives portal auto-approve + the members
        # view). Add them if missing so any install matches the live schema. SQLite has no
        # ADD COLUMN IF NOT EXISTS, so guard on PRAGMA.
        _cols = {r[1] for r in con.execute("PRAGMA table_info(rtm_mgmt_flats)").fetchall()}
        # leaseholder_email_alt1/alt2 hold up to two ALTERNATIVE emails (a second personal address, or a
        # managing agent); correspondence_address_alt1 holds a second postal address. The primary stays in
        # leaseholder_email / correspondence_address (what is on file), so all existing code that reads the
        # primary is unchanged; the alternatives are a fallback/reference only.
        # demand_cc: an optional Cc address for demand mailings (e.g. a managing agent copied in without
        # becoming the served party). payer_aliases: JSON list of alternate payer NAMES that still
        # reconcile to this flat when a bank credit for the right amount arrives with an imperfect
        # reference (e.g. a company paying on a leaseholder's behalf).
        for _col in ("greeting_name", "leaseholder_email", "leaseholder_email_alt1",
                     "leaseholder_email_alt2", "correspondence_address_alt1", "demand_cc", "payer_aliases"):
            if _col not in _cols:
                con.execute(f"ALTER TABLE rtm_mgmt_flats ADD COLUMN {_col} TEXT NOT NULL DEFAULT ''")
        # excluded=1 marks a NON-CHARGING test/fixture flat (e.g. the Flat 0 sign-up test leaseholders):
        # it is deliberately hidden from every inventory count, director view and financial summary, but
        # is still published in the portal snapshot so the sign-up + portal flow can be exercised end to
        # end. Money and votes are already zero for it (no apportionment rows, not an RTM member), so this
        # flag only governs raw "how many flats" counts. list_flats() filters it out unless asked.
        if "excluded" not in _cols:
            con.execute("ALTER TABLE rtm_mgmt_flats ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
        # sort_order drives the letter order budget lines print in on demand documents. It too was
        # hand-added to the live DB and missing from the CREATE TABLE, so a fresh install silently fell
        # back to insertion order (the alphabetical-lines demand bug). Guarantee it here.
        _bcols = {r[1] for r in con.execute("PRAGMA table_info(rtm_mgmt_budget_lines)").fetchall()}
        if "sort_order" not in _bcols:
            con.execute("ALTER TABLE rtm_mgmt_budget_lines ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
        # An independent warning cap for a line (e.g. Sundries): budget_actuals flags the line once
        # spend+committed passes this, regardless of the budgeted amount. NULL = no cap.
        if "warn_threshold_pence" not in _bcols:
            con.execute("ALTER TABLE rtm_mgmt_budget_lines ADD COLUMN warn_threshold_pence INTEGER")
        # An OWNER explanation of a bank line, kept apart from the bank's own data. An exception list is
        # only useful if answering a question makes it go away: without somewhere durable to record "that
        # £5.00 was a Docmail postage topup", every review cycle re-raises lines already explained, and a
        # reviewer soon learns to ignore the list. Attributed and timestamped so an explanation is evidence
        # rather than an anonymous edit, and never written by the importer, so it cannot be mistaken for
        # something the bank said.
        _tcols = {r[1] for r in con.execute("PRAGMA table_info(rtm_mgmt_bank_transactions)").fetchall()}
        for _c in ("notes", "explained_by", "explained_at"):
            if _c not in _tcols:
                con.execute(f"ALTER TABLE rtm_mgmt_bank_transactions ADD COLUMN {_c} TEXT NOT NULL DEFAULT ''")
        # Which bank account a row belongs to: 'current' (operating), 'reserve' or 'service' (the two
        # interest-bearing fund accounts). Every existing row is the current account, so that is the default.
        # Fund-account rows are internal transfers or interest only, and are NEVER matched to budget/collection.
        if "account" not in _tcols:
            con.execute("ALTER TABLE rtm_mgmt_bank_transactions ADD COLUMN account TEXT NOT NULL DEFAULT 'current'")
        con.commit()


# -------------------------------------------------------------------------------------------------
# Trusted-payee bank details registry (invoice-redirection fraud guard). Keep each supplier's bank
# details: the FIRST invoice records them as 'seen' (unverified); once M confirms by phone they become
# 'confirmed'. Every later invoice's details are diffed against the file. A match to a confirmed record
# is safe; details that DIFFER from a confirmed record are the single strongest redirection-fraud signal.
# Read/propose only: this never moves money.
# -------------------------------------------------------------------------------------------------

def _norm_sort(value: str) -> str:
    """Normalise a sort code to dd-dd-dd, or '' if it is not exactly 6 digits."""
    digits = re.sub(r"\D", "", value or "")
    return f"{digits[0:2]}-{digits[2:4]}-{digits[4:6]}" if len(digits) == 6 else ""


def _norm_acct(value: str) -> str:
    """Normalise an account number to its 8 digits, or '' if not exactly 8 digits."""
    digits = re.sub(r"\D", "", value or "")
    return digits if len(digits) == 8 else ""


def _payee_key(payee: str) -> str:
    """Stable identity key for a payee: an email domain if one is present, else the lowercased name with
    company suffixes and punctuation stripped, so the caller can pass a name OR a domain and still key
    the same supplier consistently."""
    p = (payee or "").strip().lower()
    m = re.search(r"@([a-z0-9.\-]+)", p)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", p):  # a bare domain like liftinspect.example
        return p
    p = re.sub(r"\b(ltd|limited|plc|llp|uk|co|company)\b", " ", p)
    return re.sub(r"[^a-z0-9]+", "", p)


def extract_bank_details(text: str) -> dict[str, str]:
    """Best-effort pull of sort code / account number / account name from an invoice's extracted text.
    Returns normalised {'sort_code','account_number','account_name'}, '' for anything not found."""
    text = text or ""
    out = {"sort_code": "", "account_number": "", "account_name": ""}
    m = re.search(r"(?i)sort\s*code[:\s]*([0-9]{2}[\-\s]?[0-9]{2}[\-\s]?[0-9]{2})", text)
    if m:
        out["sort_code"] = _norm_sort(m.group(1))
    m = re.search(r"(?i)account\s*(?:number|no\.?|#)?[:\s]*([0-9]{8})\b", text)
    if m:
        out["account_number"] = _norm_acct(m.group(1))
    m = re.search(r"(?i)account\s*name[:\s]*([A-Za-z0-9 &'.\-]{2,80}?)"
                  r"(?=\s+(?:account|sort|bank|iban|number)\b|[\r\n]|$)", text)
    if m:
        out["account_name"] = m.group(1).strip()
    return out


def record_payee_bank(payee: str, sort_code: str, account_number: str, *, account_name: str = "",
                      confirmed: bool = False, source: str = "", actor: str = "owner") -> dict[str, Any]:
    """Record (or confirm) a payee's bank details: 'seen' by default, 'confirmed' once M has verified
    them. Idempotent on (payee_key, sort_code, account_number); never downgrades a confirmed record."""
    key = _payee_key(payee)
    sc, ac = _norm_sort(sort_code), _norm_acct(account_number)
    if not key or not sc or not ac:
        return {"ok": False, "error": "need a payee plus a valid 6-digit sort code and 8-digit account number"}
    now = utc_now_iso()
    status = "confirmed" if confirmed else "seen"
    with connect() as con:
        row = con.execute(
            "SELECT id, status FROM rtm_payee_bank WHERE payee_key=? AND sort_code=? AND account_number=?",
            (key, sc, ac)).fetchone()
        if row:
            new_status = "confirmed" if (confirmed or row["status"] == "confirmed") else "seen"
            con.execute(
                "UPDATE rtm_payee_bank SET payee_name=COALESCE(NULLIF(?,''),payee_name), "
                "account_name=COALESCE(NULLIF(?,''),account_name), status=?, "
                "confirmed_by=CASE WHEN ?='confirmed' THEN ? ELSE confirmed_by END, "
                "confirmed_at=CASE WHEN ?='confirmed' AND confirmed_at IS NULL THEN ? ELSE confirmed_at END, "
                "source=COALESCE(NULLIF(?,''),source), updated_at=? WHERE id=?",
                (payee, account_name, new_status, new_status, actor, new_status, now, source, now, row["id"]))
            con.commit()
            return {"ok": True, "id": row["id"], "status": new_status, "created": False}
        con.execute(
            "INSERT INTO rtm_payee_bank(payee_key,payee_name,sort_code,account_number,account_name,status,"
            "confirmed_by,source,first_seen_at,confirmed_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (key, payee, sc, ac, account_name, status, actor if confirmed else "", source, now,
             now if confirmed else None, now))
        con.commit()
    return {"ok": True, "status": status, "created": True}


def check_invoice_bank_details(payee: str, sort_code: str, account_number: str) -> dict[str, Any]:
    """Diff an invoice's bank details against the registry. Verdict:
      match_confirmed   exactly matches a CONFIRMED record for this payee (safe to pay).
      match_unconfirmed matches a 'seen' record, but M has not confirmed it by phone yet.
      changed           this payee HAS confirmed details on file and these DIFFER (redirection-fraud signal).
      new_payee         no confirmed record for this payee (first invoice: confirm by phone).
    'on_file' lists the confirmed details when 'changed'. Read-only."""
    key = _payee_key(payee)
    sc, ac = _norm_sort(sort_code), _norm_acct(account_number)
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT sort_code, account_number, account_name, status FROM rtm_payee_bank WHERE payee_key=?",
            (key,)).fetchall()]
    if not rows:
        return {"verdict": "new_payee", "payee_key": key, "on_file": []}
    confirmed = [r for r in rows if r["status"] == "confirmed"]
    # A confirmed record for this payee is the anchor. If these details match a CONFIRMED record it's safe;
    # if the payee HAS confirmed details but these are not among them, it is 'changed' (fraud signal) even
    # if they happen to match some earlier 'seen' record, so a stored fraud attempt can never mask it.
    if any(r["sort_code"] == sc and r["account_number"] == ac for r in confirmed):
        return {"verdict": "match_confirmed", "payee_key": key}
    if confirmed:
        return {"verdict": "changed", "payee_key": key,
                "on_file": [{"sort_code": r["sort_code"], "account_number": r["account_number"],
                             "account_name": r["account_name"]} for r in confirmed]}
    # No confirmed details yet: a match to a 'seen' record is unconfirmed; otherwise it's a new sighting.
    if any(r["sort_code"] == sc and r["account_number"] == ac for r in rows):
        return {"verdict": "match_unconfirmed", "payee_key": key}
    return {"verdict": "new_payee", "payee_key": key, "on_file": []}


_BANK_VERDICT_GUIDANCE = {
    "new_payee": ("First time these bank details have been seen for this payee. Treat as UNVERIFIED: confirm "
                  "by phone on a number you already hold before paying."),
    "match_unconfirmed": "Seen before but not yet confirmed by phone. Confirm before paying.",
    "match_confirmed": "These match bank details previously CONFIRMED for this payee.",
    "changed": ("WARNING: these DIFFER from bank details previously CONFIRMED for this payee, a classic "
                "invoice-redirection fraud signal. Do NOT pay; verify by phone on a known number."),
}


def _flag_payee_task(payee_key: str, verdict: str, bd: dict[str, str]) -> None:
    """Auto-create an RTM task so a payee-bank concern outlives the chat transcript (Chat's #5). Only for
    'new_payee' (first sighting -> confirm) and 'changed' (fraud alarm); dedup-keyed on source_task_id so
    re-reading the same invoice updates rather than spamming. Best-effort; never breaks the read."""
    if verdict not in ("new_payee", "changed"):
        return
    sc, ac = bd.get("sort_code", ""), bd.get("account_number", "")
    if verdict == "changed":
        title = (f"SUSPECTED invoice fraud: bank details for {payee_key} differ from the confirmed "
                 f"account ({sc} / {ac}). Do NOT pay; verify by phone on a known number.")
        prio = "high"
    else:
        title = (f"Confirm {payee_key} bank details by phone before paying ({sc} / {ac}); first invoice "
                 "from this payee.")
        prio = "normal"
    try:
        from .calendar_tasks import add_task, TaskIn
    except ImportError:
        # mongoose-core on its own has no task list. A payee-bank warning must never vanish silently,
        # so record it as a notification instead (deduplicated by content, like the task).
        try:
            from .notifications import NotificationIn, store_notification
            store_notification(NotificationIn(source="rtm_payee_guard", app="RTM", title=title[:200],
                                              privacy="high", urgency="high" if prio == "high" else "normal"))
        except Exception:
            pass
        return
    try:
        add_task(TaskIn(source="rtm_payee_guard", source_task_id=f"payee:{payee_key}:{verdict}:{sc}:{ac}",
                        list_name="RTM", title=title[:200], priority=prio, status="open"))
    except Exception:
        pass


def assess_invoice_bank_details(text: str, payee_hint: str = "", *, record_seen: bool = True) -> dict[str, Any] | None:
    """One-shot assessment shared by every read path: extract an invoice's bank details, diff them against
    the registry, optionally record them as 'seen', auto-flag a task on a concern, and attach plain
    guidance. Returns None if the text carries no bank details. Lets the model warn from REAL history."""
    bd = extract_bank_details(text)
    if not (bd["sort_code"] and bd["account_number"]):
        return None
    payee = (payee_hint or bd.get("account_name") or "").strip()
    verdict = check_invoice_bank_details(payee, bd["sort_code"], bd["account_number"])
    if record_seen:
        try:
            record_payee_bank(payee, bd["sort_code"], bd["account_number"],
                              account_name=bd.get("account_name", ""), source="read invoice")
        except Exception:
            pass
    _flag_payee_task(verdict.get("payee_key", ""), verdict.get("verdict", ""), bd)
    return {**verdict, "extracted": bd, "guidance": _BANK_VERDICT_GUIDANCE.get(verdict.get("verdict"), "")}


def _id(prefix: str) -> str:
    return f"{prefix}_" + uuid.uuid4().hex[:12]


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_date(value: str | None) -> str:
    """Normalise a date to ISO (YYYY-MM-DD). Accepts ISO first, then common UK bank
    export formats. Metro's CSV export uses DD/MM/YYYY, so an ISO-only parser would
    throw on every row and silently bucket real payments as 'unmatched'. Kept strict:
    unknown formats raise, they are not guessed into a wrong date."""
    if not value:
        return _today()
    s = str(value).strip()
    # ISO (possibly with a time component) , the primary internal format.
    try:
        return date.fromisoformat(s[:10]).isoformat()
    except ValueError:
        pass
    # Common bank/statement formats. DD/MM before MM/DD because Metro (and UK banks)
    # are day-first; %b/%B cover "16 Jul 2026" / "16 July 2026" statement styles.
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y",
                "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date format: {value!r}")


def _add_days(value: str, days: int) -> str:
    return (date.fromisoformat(value[:10]) + timedelta(days=days)).isoformat()


def _money_to_pence(amount: str | float | int) -> int:
    """Convert pounds to integer pence using Decimal (no float drift). Integers and floats are
    treated as POUNDS consistently, no magnitude-guessing (a plain 1500 is £1,500.00, not £15.00).
    Strings may include £ and commas. This is FTT-sensitive money handling, so it is exact."""
    from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
    if isinstance(amount, bool):  # guard: bool is a subclass of int
        raise ValueError("amount must be a number, not a bool")
    if isinstance(amount, int):
        return amount * 100
    if isinstance(amount, float):
        d = Decimal(str(amount))
    else:
        s = str(amount).strip().replace("\u00a3", "").replace(",", "")
        if not s:
            raise ValueError("amount required")
        try:
            d = Decimal(s)
        except InvalidOperation:
            raise ValueError(f"not a valid money amount: {amount!r}")
    return int(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def _money(pence: int | None) -> str:
    if pence is None:
        return ""
    sign = "-" if int(pence) < 0 else ""
    p = abs(int(pence))
    return f"{sign}£{p//100:,}.{p%100:02d}"


def _norm_schedule(value: str | None) -> str:
    s = (value or "unknown").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {"sch_1": "sch1", "schedule_1": "sch1", "schedule1": "sch1",
               "sch_2": "sch2", "schedule_2": "sch2", "schedule2": "sch2",
               "oneoff": "one_off", "one": "one_off"}
    s = aliases.get(s, s)
    return s if s in SCHEDULES else "unknown"


def _site_key() -> tuple[Any, ...]:
    """Cache key for values derived from the site profile: the env vars that select the profile plus a
    2-second time bucket. The profile accessors re-resolve the profile path through get_settings()
    (which re-reads .env) on every call, too slow for the per-bank-line and per-flat loops here."""
    import os
    import time
    return (os.environ.get("MONGOOSE_PROFILE_FILE"), os.environ.get("MONGOOSE_DB_PATH"),
            os.environ.get("MONGOOSE_DATA_DIR"), int(time.monotonic() // 2))


@lru_cache(maxsize=8)
def _site_for(_key: tuple[Any, ...]) -> dict[str, Any]:
    codes = tuple(_profile.block_codes())
    spellings: dict[str, str] = {}
    for b in _profile.blocks():
        # Every way the director writes a block (its number, its code, a street alias), upper-cased
        # with spaces removed, mapped to the block code.
        for k in (b["number"], b["code"], *b["aliases"]):
            spellings[str(k).upper().replace(" ", "")] = b["code"]
    name = _profile.rtm_name()
    return {"codes": codes, "code_re": _profile.block_code_regex(), "spellings": spellings,
            "name": name, "name_words": frozenset(w.lower() for w in name.split())}


def _site() -> dict[str, Any]:
    """The site profile facts this module uses: block codes, block spellings, the RTM company name."""
    return _site_for(_site_key())


def _block_spellings() -> dict[str, str]:
    return _site()["spellings"]


def _flat_id_shape_hint() -> str:
    """'flat must look like <first>F1 or <last>F9' wording, from the profile's block codes."""
    codes = _site()["codes"]
    return f"{codes[0]}F1 or {codes[-1]}F9"


def _flat_id(block: str, flat_number: str | int) -> str:
    b = str(block).strip().upper().replace(" ", "")
    b = _block_spellings().get(b, b)
    n = int(str(flat_number).strip().replace("Flat", "").replace("flat", ""))
    return f"{b}F{n}"


def seed_flats() -> dict[str, Any]:
    """Create placeholder reference rows for every profile block's flats 1-9 without inventing names."""
    _ensure_tables()
    now = utc_now_iso()
    count = 0
    with connect() as con:
        for block in _site()["codes"]:
            for n in range(1, 10):
                fid = _flat_id(block, n)
                doc_scheme = f"{{YY}}-TO-{{MON}}/{block}F{n}R{{revision}}"
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO rtm_mgmt_flats(flat_id,block,flat_number,leaseholder_name,
                        correspondence_address,apportionment_band,payment_reference,document_scheme,notes,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (fid, block, n, "", "", "unknown", "", doc_scheme,
                     "Placeholder. Fill leaseholder/address/payment reference before live demands.", now, now),
                )
                count += cur.rowcount
    return {"ok": True, "inserted": count, "total": len(list_flats())}


def _ref_warnings(payment_reference: str) -> list[str]:
    """Non-blocking checks on a payment reference against UK Open Banking constraints.
    Warns, never rejects, the director stays in control. The matcher tolerates messy
    inbound strings, but a clean, short, OB-safe reference on the DEMAND is what stops
    truncation/ambiguity arising at all."""
    warns: list[str] = []
    ref = (payment_reference or "").strip()
    if not ref:
        return warns
    OB_MAX = 18
    if len(ref) > OB_MAX:
        warns.append(
            f"Reference {ref!r} is {len(ref)} chars; UK Open Banking caps the reference "
            f"field at {OB_MAX}, so a bank may truncate it to {ref[:OB_MAX]!r}. "
            f"Consider a shorter token (e.g. a surname+flat or a flat code).")
    canon = _canon_ref(ref)
    if not canon:
        warns.append(f"Reference {ref!r} has no letters/digits; it cannot be matched.")
    elif canon != ref.lower().replace(" ", ""):
        # purely informational: show what the matcher will compare on
        warns.append(f"Reference will be matched canonically as {canon.upper()!r} "
                     f"(spacing/punctuation ignored).")
    return warns


def upsert_flat(*, block: str, flat_number: str | int, leaseholder_name: str = "",
                correspondence_address: str = "", apportionment_band: str = "",
                payment_reference: str = "", notes: str = "") -> dict[str, Any]:
    _ensure_tables()
    fid = _flat_id(block, flat_number)
    now = utc_now_iso()
    ref_warnings = _ref_warnings(payment_reference)
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_mgmt_flats(flat_id,block,flat_number,leaseholder_name,correspondence_address,
                apportionment_band,payment_reference,document_scheme,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(flat_id) DO UPDATE SET leaseholder_name=excluded.leaseholder_name,
                correspondence_address=excluded.correspondence_address, apportionment_band=excluded.apportionment_band,
                payment_reference=excluded.payment_reference, notes=excluded.notes, updated_at=excluded.updated_at
            """,
            (fid, fid[:-2] if fid.startswith(_site()["codes"]) else str(block).upper(), int(flat_number),
             leaseholder_name, correspondence_address, apportionment_band, payment_reference,
             f"{{YY}}-TO-{{MON}}/{fid}R{{revision}}", notes, now, now),
        )
    out = {"ok": True, "flat_id": fid}
    if ref_warnings:
        out["reference_warnings"] = ref_warnings
    return out


def list_flats(include_excluded: bool = False) -> list[dict[str, Any]]:
    """All flats, ordered by block then number. By default this EXCLUDES test/fixture flats (excluded=1)
    so every count, director view and financial summary sees only the real charging flats. The portal
    snapshot builder passes include_excluded=True so the test flats can still be provisioned and shown
    their own (zero) portal."""
    _ensure_tables()
    where = "" if include_excluded else "WHERE COALESCE(excluded, 0) = 0"
    with connect() as con:
        return [dict(r) for r in con.execute(
            f"SELECT * FROM rtm_mgmt_flats {where} ORDER BY block, flat_number").fetchall()]


# Test/fixture flats (excluded=1) exist so sign-up, invites and the portal can be exercised without
# touching a real leaseholder. They must never reach a COUNT, a total or a director list, or the
# building appears to have 21 flats instead of 18 and every money figure is quietly overstated.
# list_flats() already filters; demands do not carry the flag, so anything counting DEMANDS has to
# join back to the flat. This is that join, in one place, so a new query cannot forget it.
_REAL_FLATS_ONLY = ("flat_id IN (SELECT flat_id FROM rtm_mgmt_flats WHERE COALESCE(excluded, 0) = 0)")


def excluded_flat_ids() -> set[str]:
    """The test/fixture flat ids, for callers that filter in Python rather than SQL."""
    _ensure_tables()
    with connect() as con:
        return {str(r["flat_id"]).upper() for r in con.execute(
            "SELECT flat_id FROM rtm_mgmt_flats WHERE COALESCE(excluded, 0) = 1").fetchall()}


def _get_flat(flat_id: str) -> dict[str, Any] | None:
    _ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM rtm_mgmt_flats WHERE flat_id=?", (flat_id,)).fetchone()
        return dict(row) if row else None


def _ledger_entry(*, flat_id: str | None, entry_date: str, kind: str, description: str,
                  debit_pence: int = 0, credit_pence: int = 0, document_number: str = "",
                  bank_transaction_id: str = "", payable_id: str = "", schedule: str = "sch1",
                  fund: str = "service_charge_trust", correction_of: str = "", note: str = "") -> str:
    _ensure_tables()
    eid = _id("led")
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_mgmt_ledger_entries(id,flat_id,fund,schedule,entry_date,kind,description,
                debit_pence,credit_pence,document_number,bank_transaction_id,payable_id,correction_of,immutable_note,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (eid, flat_id, fund, _norm_schedule(schedule), entry_date, kind, description, abs(int(debit_pence)),
             abs(int(credit_pence)), document_number, bank_transaction_id, payable_id, correction_of, note, utc_now_iso()),
        )
    return eid


def create_demand(*, document_number: str, flat: str, period: str, amount: str | float | int,
                  issue_date: str | None = None, due_date: str | None = None,
                  delivery_method: str = "", revision: int = 0, supersedes: str = "",
                  schedule: str = "sch1", notes: str = "",
                  allow_incomplete: bool = False) -> dict[str, Any]:
    _ensure_tables()
    flat_id = flat.strip().upper().replace("-", "")
    # Accept <code>F1, <code>-F1, or block+number-ish strings (codes from the site profile).
    flat_id = flat_id.replace(" ", "")
    m = re.fullmatch(rf"({_site()['code_re']})F(\d+)", flat_id)
    if not m:
        raise ValueError(f"flat must look like {_flat_id_shape_hint()}")
    flat_id = _flat_id(m.group(1), m.group(2))
    if not _get_flat(flat_id):
        seed_flats()
    fl = _get_flat(flat_id) or {}
    # NOTE on the placeholder-demand risk (external audit B1): create_demand deliberately
    # does NOT gate on flat completeness. This subsystem's own extensive test suite creates
    # demands against unnamed/auto-seeded flats as a normal, established pattern (bank
    # reconciliation, portal claims, ledger mechanics), so a hard block here broke ~26
    # legitimate call sites. The actual safety property that matters, "a leaseholder must
    # never SEE a demand against a nameless placeholder flat", is enforced where a leaseholder
    # can actually see something: rtm_portal._demand_rows_for_snapshot excludes any demand
    # whose flat has no leaseholder_name from the published portal snapshot. allow_incomplete
    # is accepted for API compatibility with that call site but no longer changes behaviour.
    status = "Issued"
    issued = _parse_date(issue_date)
    due = _parse_date(due_date) if due_date else _add_days(issued, 28)
    pence = _money_to_pence(amount)
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_mgmt_demands(document_number,flat_id,leaseholder_name,period,issue_date,due_date,
                amount_pence,delivery_method,revision,status,supersedes_document_number,schedule,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (document_number, flat_id, fl.get("leaseholder_name", ""), period, issued, due, pence,
             delivery_method, int(revision), status, supersedes, _norm_schedule(schedule), notes, now, now),
        )
        if supersedes:
            con.execute("UPDATE rtm_mgmt_demands SET status='Superseded', updated_at=? WHERE document_number=?", (now, supersedes))
            # IMMUTABILITY: do not delete or edit the superseded demand's ledger posting , reverse it
            # with an equal-and-opposite credit so the per-flat balance reflects ONLY the live demand
            # (otherwise both debits remain and the balance double-counts). The original entry stays
            # for the audit trail; the reversal links back via correction_of.
            old = con.execute(
                "SELECT id, debit_pence, schedule, fund FROM rtm_mgmt_ledger_entries "
                "WHERE document_number=? AND kind='demand' AND debit_pence>0",
                (supersedes,)).fetchone()
    if supersedes and old:
        _ledger_entry(flat_id=flat_id, entry_date=issued, kind="reversal",
                      description=f"Reversal of superseded demand {supersedes}",
                      credit_pence=old["debit_pence"], schedule=old["schedule"],
                      fund=old["fund"], correction_of=old["id"],
                      note=f"Reverses ledger debit of superseded demand {supersedes}.")
    _ledger_entry(flat_id=flat_id, entry_date=issued, kind="demand", description=f"Demand {document_number} {period}",
                  debit_pence=pence, document_number=document_number, schedule=schedule,
                  note="Demand raised. Corrections must be new reversing entries, not edits.")
    # Apply any standing on-account credit this flat holds (an advance for exactly this next demand, or a
    # carried-forward overpayment). Ledger-neutral: it writes a credit-application row, not a ledger entry,
    # so the flat's balance moves by only the demand debit while this demand reads Paid/Part-paid.
    applied = _apply_available_credit(flat_id, document_number)
    status = refresh_demand_status(document_number)
    return {"ok": True, "document_number": document_number, "flat_id": flat_id,
            "amount": _money(pence), "due_date": due, "status": status,
            "on_account_applied": _money(applied) if applied else None}



def list_periods() -> dict[str, Any]:
    """Every charging period that exists, newest first, plus which one is CURRENT and per-period totals.

    One source of truth for the period selector on both surfaces (Cockpit and the WordPress plugin), so
    they can never disagree about what periods exist or which is current. "Current" = the period holding
    the most recent due_date, which is what a director means by "this cycle"; everything older is
    brought forward. Totals are pence-exact from the same ledger arithmetic as the dashboard.
    """
    _ensure_tables()
    with connect() as con:
        rows = con.execute(
            "SELECT period, COUNT(*) n, COALESCE(SUM(amount_pence),0) charged, MAX(due_date) last_due "
            "FROM rtm_mgmt_demands WHERE status NOT IN ('Superseded','Cancelled') "
            f"AND COALESCE(period,'')!='' AND {_REAL_FLATS_ONLY} "
            "GROUP BY period ORDER BY MAX(due_date) DESC, period DESC"
        ).fetchall()
        alloc = {r["period"]: int(r["s"] or 0) for r in con.execute(
            "SELECT d.period period, COALESCE(SUM(x.amount_pence),0) s FROM "
            "(SELECT document_number, amount_pence FROM rtm_mgmt_allocations "
            " UNION ALL SELECT document_number, amount_pence FROM rtm_mgmt_credit_applications) x "
            "JOIN rtm_mgmt_demands d ON d.document_number=x.document_number GROUP BY d.period"
        ).fetchall()}
    periods = []
    for r in rows:
        charged = int(r["charged"] or 0)
        paid = int(alloc.get(r["period"], 0))
        # Clamp at 0: a period the flats have overpaid/prepaid must read £0 owed, never a negative "owed".
        outstanding = max(0, charged - paid)
        periods.append({"period": r["period"], "demands": int(r["n"] or 0),
                        "charged": _money(charged), "paid": _money(paid),
                        "outstanding": _money(outstanding), "outstanding_pence": outstanding,
                        "prepaid_pence": max(0, paid - charged),
                        "last_due_date": r["last_due"] or ""})
    # "current" = newest well-formed charging period. `periods` is already newest-first, so pick the first
    # whose label matches the canonical half-year grammar (YYYY-H1 / YYYY-H2). A stray/malformed label
    # (e.g. a "p" a test once injected) is still listed but can NEVER be selected as current, so the
    # portal budget bar cannot be hijacked into treating a bogus period as "this cycle". Fall back to the
    # newest period only if none are well-formed, which preserves prior behaviour for real data.
    _canon = re.compile(r"^\d{4}-H[12]$")
    current = ""
    for _p in periods:
        if _canon.match(str(_p["period"])):
            current = _p["period"]
            break
    if not current and periods:
        current = periods[0]["period"]
    return {"ok": True, "periods": periods, "current": current, "count": len(periods)}


def list_demands(status: str | None = None, flat: str | None = None, limit: int = 200,
                 period: str | None = None) -> list[dict[str, Any]]:
    """Demands, optionally narrowed. `period` takes a canonical label like '2026-H2'. Without it every
    period is returned, which is right for a ledger total but NOT for a director reading one charging
    cycle: two live periods otherwise interleave into a single undifferentiated list."""
    _ensure_tables()
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if flat:
        clauses.append("flat_id=?")
        params.append(flat.strip().upper().replace("-", ""))
    if period:
        clauses.append("period=?")
        params.append(period.strip())
    # Test/fixture flats never belong in a director's demand list or its count. Asking for a specific
    # flat still works (so a test flat can be inspected deliberately), but the general list is real-only.
    if not flat:
        clauses.append(_REAL_FLATS_ONLY)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"SELECT * FROM rtm_mgmt_demands {where} ORDER BY due_date DESC, document_number LIMIT ?",
            (*params, max(1, min(limit, 5000))),
        ).fetchall()
        # The payment reference lives on the flat, not the demand, but a demand list is useless without it
        # (you cannot match a bank credit), so attach it here from a single flats lookup.
        ref_by_flat = {str(f["flat_id"]): f["payment_reference"]
                       for f in con.execute("SELECT flat_id, payment_reference FROM rtm_mgmt_flats").fetchall()}
    out = []
    for r in rows:
        d = dict(r)
        paid = allocated_to_demand(d["document_number"])
        d["amount"] = _money(d["amount_pence"])
        d["allocated"] = _money(paid)
        d["outstanding_pence"] = max(0, d["amount_pence"] - paid)
        d["outstanding"] = _money(d["outstanding_pence"])
        d["payment_reference"] = ref_by_flat.get(str(d.get("flat_id")))
        out.append(d)
    return out


def allocated_to_demand(document_number: str) -> int:
    _ensure_tables()
    with connect() as con:
        row = con.execute(
            "SELECT (SELECT COALESCE(SUM(amount_pence),0) FROM rtm_mgmt_allocations WHERE document_number=?)"
            "     + (SELECT COALESCE(SUM(amount_pence),0) FROM rtm_mgmt_credit_applications WHERE document_number=?) s",
            (document_number, document_number)).fetchone()
        return int(row["s"] or 0)


# ── Payment on account (advance / overpayment credit) ─────────────────────────────────────────────────
#
# A leaseholder may pay before a demand exists (next half-year's budget), or pay more than the current
# demand. The owner's rule is that this is ALWAYS a payment on account: there are no refunds, so any money
# beyond what is currently demanded is a standing CREDIT that carries forward and is applied to the next
# demand raised. It is held fund-undesignated ('on_account' schedule) until it lands on a real demand,
# whose reserve/service proportions it then takes; the owner may pin it to a fund early (assign_on_account_
# to_fund). Represented as a normal ledger CREDIT (kind='on_account', blank document_number) so
# flat_statement shows the flat in credit for free; applied to a demand via a credit-application row (not a
# second ledger entry) so allocated_to_demand nets it and the demand reads Paid without double-counting.

_ON_ACCOUNT_KINDS = ("on_account", "credit_note")


def available_on_account_credit(flat_id: str) -> int:
    """Pence of standing credit this flat has that is NOT yet consumed by a live demand.

    Derived, never stored: total on-account/credit-note ledger credit MINUS credit already applied to
    demands that are still live (a superseded/cancelled demand releases its application, so re-derivation
    re-applies it to the new revision without double-consuming). This is what makes auto-apply idempotent.
    """
    _ensure_tables()
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    with connect() as con:
        # NET credit-debit: a fund reassignment (assign_on_account_to_fund) posts an equal debit+credit
        # pair on these kinds, so summing credit alone would double-count it. Netting keeps it invariant.
        posted = con.execute(
            f"SELECT COALESCE(SUM(credit_pence),0) - COALESCE(SUM(debit_pence),0) s FROM rtm_mgmt_ledger_entries "
            f"WHERE flat_id=? AND kind IN ({','.join('?' for _ in _ON_ACCOUNT_KINDS)})",
            (fid, *_ON_ACCOUNT_KINDS)).fetchone()["s"] or 0
        applied = con.execute(
            "SELECT COALESCE(SUM(ca.amount_pence),0) s FROM rtm_mgmt_credit_applications ca "
            "JOIN rtm_mgmt_demands d ON d.document_number=ca.document_number "
            "WHERE ca.flat_id=? AND d.status NOT IN ('Superseded','Cancelled')",
            (fid,)).fetchone()["s"] or 0
    return max(0, int(posted) - int(applied))


def record_on_account_credit(*, flat: str, amount: Any, received_date: str | None = None,
                             reference: str = "", method: str = "Bank transfer", note: str = "",
                             fund: str = "service_charge_trust", schedule: str = "on_account") -> dict[str, Any]:
    """Record money received on account (no demand, or the excess of an overpayment). Books a bank credit
    (unallocated, so bank reconciliation can still fingerprint-dedupe the real Metro line) plus one
    on-account ledger credit that drives the flat's balance. Never allocates and never refunds."""
    _ensure_tables()
    fid = (flat or "").strip().upper().replace("-", "").replace(" ", "")
    if not _get_flat(fid):
        return {"ok": False, "error": f"unknown flat {flat!r}"}
    pence = _money_to_pence(amount) if amount not in (None, "") else 0
    if pence <= 0:
        return {"ok": False, "error": "amount must be positive"}
    fl = _get_flat(fid) or {}
    ref = (reference or str(fl.get("payment_reference") or "")).strip()
    name = str(fl.get("leaseholder_name") or fid)
    dt = _parse_date(received_date)
    txid = _id("bank")
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """INSERT INTO rtm_mgmt_bank_transactions(id,bank_date,description,reference,amount_pence,direction,
               match_status,matched_flat_id,matched_document_number,allocated_pence,duplicate_of,raw_json,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (txid, dt, f"Payment on account, {name} ({fid})" + (f", {note}" if note else ""), ref, pence,
             "credit", "reviewed", fid, "", 0, "", json.dumps({"method": method, "on_account": True}), "manual_entry", now, now))
    _ledger_entry(flat_id=fid, entry_date=dt, kind="on_account",
                  description="Payment on account" + (f" ({note})" if note else ""),
                  credit_pence=pence, document_number="", bank_transaction_id=txid,
                  fund=fund, schedule=schedule,
                  note="Payment on account. No refunds; carried forward and applied to the next demand raised.")
    # Flow it straight onto any demand already open (oldest first), so recording an advance against a flat
    # that unexpectedly has an open demand settles that demand rather than sitting past it.
    _apply_credit_to_open_demands(fid)
    st = flat_statement(fid)
    _refresh_wp_mirror_async()
    return {"ok": True, "flat_id": fid, "amount": _money(pence), "on_account": True,
            "balance": st.get("balance"), "position": st.get("position"),
            "credit_pence": abs(min(0, int(st.get("balance_pence") or 0))),
            "available_credit": _money(available_on_account_credit(fid))}


def _apply_available_credit(flat_id: str, document_number: str) -> int:
    """Apply this flat's standing on-account credit to ONE demand, capped at that demand's outstanding.

    Writes a credit-application row (NOT a ledger entry) so allocated_to_demand nets it and the demand
    reads Paid/Part-paid, without posting a second ledger credit (which would double-count the money).
    Returns pence applied. Idempotent: available credit is re-derived each call."""
    _ensure_tables()
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    avail = available_on_account_credit(fid)
    if avail <= 0:
        return 0
    with connect() as con:
        d = con.execute("SELECT amount_pence, status FROM rtm_mgmt_demands WHERE document_number=?", (document_number,)).fetchone()
        if not d or str(d["status"]) in {"Superseded", "Cancelled"}:
            return 0
    outstanding = max(0, int(d["amount_pence"]) - allocated_to_demand(document_number))
    amt = min(avail, outstanding)
    if amt <= 0:
        return 0
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_mgmt_credit_applications(id,document_number,flat_id,amount_pence,credit_entry_id,notes,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (_id("capp"), document_number, fid, amt, "", "on-account credit applied", now))
    refresh_demand_status(document_number)
    return amt


def _apply_credit_to_open_demands(flat_id: str) -> int:
    """Sweep this flat's standing credit across its open demands, oldest first, until credit runs out or
    no open demand remains. The surplus (a genuine advance for a period not yet demanded) stays as credit."""
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    applied = 0
    with connect() as con:
        docs = [r["document_number"] for r in con.execute(
            "SELECT document_number FROM rtm_mgmt_demands WHERE flat_id=? "
            "AND status NOT IN ('Paid','Superseded','Cancelled') ORDER BY due_date, revision", (fid,)).fetchall()]
    for doc in docs:
        if available_on_account_credit(fid) <= 0:
            break
        applied += _apply_available_credit(fid, doc)
    return applied


def assign_on_account_to_fund(*, flat: str, fund_schedule: str, amount: Any = None) -> dict[str, Any]:
    """Owner lever: pin standing on-account credit to a fund/schedule (e.g. 'reserve' or 'service') before
    a demand consumes it, instead of letting it default to the raised demand's proportions. Re-tags up to
    `amount` (default: all undesignated on-account credit) by posting a fund transfer pair on the ledger,
    leaving the flat balance unchanged. Only ever moves credit that is still on account (not yet applied)."""
    _ensure_tables()
    fid = (flat or "").strip().upper().replace("-", "").replace(" ", "")
    target = _norm_schedule(fund_schedule)
    avail = available_on_account_credit(fid)
    if avail <= 0:
        return {"ok": False, "error": f"{fid} has no on-account credit to assign"}
    move = _money_to_pence(amount) if amount not in (None, "") else avail
    move = min(max(0, move), avail)
    if move <= 0:
        return {"ok": False, "error": "amount must be positive"}
    now = utc_now_iso()
    # Debit the undesignated on-account pool and credit the chosen schedule: net-zero to the flat balance,
    # but the credit now carries the owner's chosen schedule so auto-apply honours it.
    _ledger_entry(flat_id=fid, entry_date=now[:10], kind="on_account", schedule="on_account",
                  description=f"On-account credit assigned to {target}", debit_pence=move, document_number="",
                  note="Owner reassigned on-account credit to a fund. Balance unchanged.")
    _ledger_entry(flat_id=fid, entry_date=now[:10], kind="on_account", schedule=target,
                  description=f"On-account credit designated {target}", credit_pence=move, document_number="",
                  note="Owner-designated on-account credit. Applied to the next demand's matching schedule.")
    st = flat_statement(fid)
    return {"ok": True, "flat_id": fid, "assigned": _money(move), "schedule": target,
            "balance": st.get("balance"), "position": st.get("position")}


def refresh_demand_status(document_number: str, today: str | None = None) -> str:
    _ensure_tables()
    today = today or _today()
    with connect() as con:
        row = con.execute("SELECT * FROM rtm_mgmt_demands WHERE document_number=?", (document_number,)).fetchone()
        if not row:
            raise ValueError("unknown demand")
        d = dict(row)
        if d["status"] in {"Superseded", "Cancelled", "In recovery"}:
            return d["status"]
        paid = allocated_to_demand(document_number)
        if int(d["amount_pence"]) <= 0:
            # A zero demand has nothing to pay: it is NOT "Paid" (nothing was ever paid), it is simply an
            # issued nil demand. Marking it Paid is misleading (a £0 test/nil demand read as settled).
            status = "Issued"
        elif paid >= int(d["amount_pence"]):
            status = "Paid"
        elif paid > 0:
            status = "Part-paid"
        elif date.fromisoformat(d["due_date"]) < date.fromisoformat(today[:10]):
            status = "Overdue"
        else:
            status = "Issued"
        con.execute("UPDATE rtm_mgmt_demands SET status=?, updated_at=? WHERE document_number=?", (status, utc_now_iso(), document_number))
    return status


_LAST_STATUS_REFRESH: dict[str, Any] = {"at": 0.0, "today": None, "result": None}


def refresh_all_demand_statuses(today: str | None = None, *, force: bool = True) -> dict[str, Any]:
    """Recompute every demand's status. This is write-heavy (a per-demand recompute), so the director
    dashboard, which calls it via BOTH fund_position and arrears in one page load, debounces it with
    force=False: if it already ran for the same 'today' within the last few seconds, reuse that result
    instead of recomputing 2-3x per load. Mutations and the scheduler keep force=True so a real change
    always triggers a real refresh."""
    _ensure_tables()
    import time as _t
    now = _t.monotonic()
    # Normalise so callers that pass None vs the resolved date share one debounce key (the dashboard's
    # fund_position passes None while arrears passes today_s , without this they'd never dedupe).
    today = today or _today()
    if (not force and _LAST_STATUS_REFRESH["result"] is not None
            and _LAST_STATUS_REFRESH["today"] == today
            and (now - _LAST_STATUS_REFRESH["at"]) < 5.0):
        return _LAST_STATUS_REFRESH["result"]
    # Single connection + one grouped allocations query, instead of the old N+1 (each demand opened its
    # own connection AND a second one for allocations). Same status logic as refresh_demand_status.
    counts: dict[str, int] = {}
    now_iso = utc_now_iso()
    today_d = date.fromisoformat(today[:10])
    with connect() as con:
        demands = [dict(r) for r in con.execute(
            "SELECT document_number, status, amount_pence, due_date FROM rtm_mgmt_demands").fetchall()]
        alloc = {r["document_number"]: int(r["s"] or 0) for r in con.execute(
            "SELECT document_number, COALESCE(SUM(amount_pence),0) s FROM "
            "(SELECT document_number, amount_pence FROM rtm_mgmt_allocations "
            " UNION ALL SELECT document_number, amount_pence FROM rtm_mgmt_credit_applications) "
            "GROUP BY document_number").fetchall()}
        for d in demands:
            cur = d["status"]
            if cur in {"Superseded", "Cancelled", "In recovery"}:
                counts[cur] = counts.get(cur, 0) + 1
                continue
            paid = alloc.get(d["document_number"], 0)
            amt = int(d["amount_pence"])
            if amt <= 0:
                status = "Issued"
            elif paid >= amt:
                status = "Paid"
            elif paid > 0:
                status = "Part-paid"
            else:
                try:
                    overdue = date.fromisoformat(d["due_date"]) < today_d
                except Exception:
                    overdue = False
                status = "Overdue" if overdue else "Issued"
            counts[status] = counts.get(status, 0) + 1
            if status != cur:
                con.execute("UPDATE rtm_mgmt_demands SET status=?, updated_at=? WHERE document_number=?",
                            (status, now_iso, d["document_number"]))
    result = {"ok": True, "updated": len(demands), "status_counts": counts}
    _LAST_STATUS_REFRESH.update(at=now, today=today, result=result)
    return result


def flat_statement(flat: str) -> dict[str, Any]:
    _ensure_tables()
    fid = flat.strip().upper().replace("-", "")
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM rtm_mgmt_ledger_entries WHERE flat_id=? ORDER BY entry_date, created_at, id", (fid,)
        ).fetchall()
    running = 0
    entries = []
    for r in rows:
        d = dict(r)
        signed = int(d["debit_pence"]) - int(d["credit_pence"])
        running += signed
        d["signed_pence"] = signed
        d["signed"] = _money(signed)
        d["running_pence"] = running
        d["running"] = _money(running)
        entries.append(d)
    in_credit = running < 0
    return {"flat_id": fid, "balance_pence": running, "balance": _money(running),
            "position": "debit" if running > 0 else "credit" if running < 0 else "clear",
            # Explicit, sign-safe credit fields: displays show credit_display without re-deriving the sign.
            "in_credit": in_credit, "credit_pence": (-running if in_credit else 0),
            "credit_display": _money(-running) if in_credit else _money(0),
            "on_account_pence": available_on_account_credit(fid),
            "on_account": _money(available_on_account_credit(fid)),
            "entries": entries}


def agent_cc_for_editing(flat: dict[str, Any]) -> str:
    """The managing agent's copy address EXACTLY as stored, for the contact-details editor only.

    An editor must show and save the raw field. Anything that SENDS mail must use correspondence_cc(),
    which merges the agent with the leaseholder's spare addresses; saving that merged list back into this
    field would corrupt it. Both readers live here, next to the column, so no other module reads it."""
    return str((flat or {}).get("demand_cc") or "")


def correspondence_cc(flat_id: str) -> list[str]:
    """Who else is copied on ANY correspondence to this flat. THE single answer to that question.

    A leaseholder's managing agent is a party to the correspondence, not to one document type. Storing that
    in a column called `demand_cc` encoded the opposite, and the result was exactly what the leaseholders
    reported: Becker's agent was copied on his DEMAND and not on his RECEIPT, because the demand mailer read
    the column and the receipt sender had never heard of it. Every outbound path now calls this.

    EVERY address on file is used: the primary goes in To, and everything else is copied. That is the
    owner's rule, and it settles what "alternate" meant. Those columns were previously inert: Petrov had
    three confirmed addresses and received everything at one, because nothing sent to alt1/alt2, nothing
    fell back to them, and nothing even used them to recognise him on the way in. Recording an address and
    never using it is worse than not asking for it, since both sides believe it is live.

    So both kinds of extra address are copied, for different reasons that happen to want the same treatment:
    a DIFFERENT party (Becker's managing agent) needs to see the correspondence, and ANOTHER ADDRESS FOR
    THE SAME PERSON (Petrov's two spares) is where that person may actually be reading. The de-duplication
    below is what stops the same human being listed twice.
    """
    fl = _get_flat(flat_id) or {}
    primary = str(fl.get("leaseholder_email") or "").strip().lower()
    return _dedupe_addrs((fl.get("demand_cc"), fl.get("leaseholder_email_alt1"),
                          fl.get("leaseholder_email_alt2")), primary)


def _dedupe_addrs(raw_fields, primary: str) -> list[str]:
    """Parse a set of comma/semicolon address fields into a clean, de-duplicated list that never contains
    the primary recipient. Shared by correspondence_cc (agent + alts) and leaseholder_alt_emails (alts
    only) so both validate identically."""
    out: list[str] = []
    for field in raw_fields:
        for raw in str(field or "").replace(";", ",").split(","):
            addr = raw.strip()
            # Reject anything that is not plausibly an address rather than handing it to the mail server.
            if not addr or "@" not in addr or "." not in addr.rsplit("@", 1)[-1]:
                continue
            # Never copy the primary recipient. A duplicate in the CC line reads as carelessness to the
            # leaseholder and can double-deliver on some clients.
            if addr.lower() == primary:
                continue
            if addr.lower() not in [x.lower() for x in out]:
                out.append(addr)
    return out


_CONTACT_FIELDS = ("leaseholder_name", "greeting_name", "leaseholder_email", "leaseholder_email_alt1",
                   "leaseholder_email_alt2", "demand_cc", "payment_reference", "correspondence_address",
                   "correspondence_address_alt1")
_CONTACT_EMAIL_FIELDS = ("leaseholder_email", "leaseholder_email_alt1", "leaseholder_email_alt2", "demand_cc")


def _looks_like_email(a: str) -> bool:
    import re as _re
    return bool(_re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (a or "").strip()))


def set_flat_contact(flat_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Update a flat's CONTACT details (names, emails, managing-agent cc, payment reference, addresses).

    Only the whitelisted fields are writable, and only the ones actually supplied are changed. Email fields
    must be a valid address or empty (empty clears an alt). The primary email cannot be blanked (it is the
    served party and drives portal auto-approve). Returns the applied fields, or a per-field error.
    """
    _ensure_tables()
    fid = (flat_id or "").strip().upper().replace("-", "").replace(" ", "")
    fl = _get_flat(fid)
    if not fl:
        return {"ok": False, "error": f"unknown flat {flat_id!r}"}
    if not isinstance(fields, dict):
        return {"ok": False, "error": "fields must be an object"}
    updates: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, raw in fields.items():
        col = str(key).strip()
        if col not in _CONTACT_FIELDS:
            continue  # ignore anything not a known contact field (never write arbitrary columns)
        val = ("" if raw is None else str(raw)).strip()
        if col in _CONTACT_EMAIL_FIELDS and val and not _looks_like_email(val):
            errors[col] = f"{val!r} is not a valid email"
            continue
        if col == "leaseholder_email" and not val:
            errors[col] = "the primary email cannot be blank"
            continue
        if col == "payment_reference":
            val = val.upper()
        updates[col] = val[:500]
    if errors and not updates:
        return {"ok": False, "errors": errors}
    if not updates:
        return {"ok": True, "flat_id": fid, "applied": {}, "unchanged": True, "errors": errors}
    # Only write cells that actually change, so a no-op save does not churn updated_at or the snapshot.
    changed = {c: v for c, v in updates.items() if str(fl.get(c) or "") != v}
    if changed:
        set_cols = ", ".join(f"{c}=?" for c in changed) + ", updated_at=?"
        with connect() as con:
            con.execute(f"UPDATE rtm_mgmt_flats SET {set_cols} WHERE flat_id=?",
                        (*[changed[c] for c in changed], utc_now_iso(), fid))
            con.commit()
        try:
            audit("rtm_contact", "update", True, payload={"flat": fid, "fields": sorted(changed)})
        except Exception:
            pass
    return {"ok": True, "flat_id": fid, "applied": changed, "errors": errors}


def leaseholder_alt_emails(flat_id: str) -> list[str]:
    """The leaseholder's OWN alternative email addresses (alt1/alt2), for a building-wide broadcast.

    Deliberately narrower than correspondence_cc: a general announcement (and any photographs on it) copies
    the leaseholder's own spare inboxes, but NOT their managing agent (demand_cc). A third party who acts on
    that leaseholder's service charge does not need every rubbish-clearout notice for the building, and
    copying them would be a wider disclosure than the director asked for. Financial correspondence still
    uses the full correspondence_cc, where the agent is a proper party."""
    fl = _get_flat(flat_id) or {}
    primary = str(fl.get("leaseholder_email") or "").strip().lower()
    return _dedupe_addrs((fl.get("leaseholder_email_alt1"), fl.get("leaseholder_email_alt2")), primary)


def collection_position() -> dict[str, Any]:
    """Collection, told honestly where payment plans exist.

    A single "82% collected" bar misleads once arrears are being paid by agreement. That money is not lost,
    it is deferred with the directors' consent, so it belongs in its own band rather than lumped in with
    genuine default. Four bands, which sum to what was demanded:

      collected      cash actually received, INCLUDING plan instalments already paid
      plan_deferred  still owed under an agreed plan and not yet due (light beige: what would have been
                     collected by now had no plan been granted)
      plan_paid      the part of the plan already paid (grey: a subset of `collected`, reported separately
                     so the bar can show progress through the plan)
      unpaid         owed with no plan, or under a broken plan: the real arrears

    Two percentages, because they answer different questions. percent_collected is where the money is today
    and counts plan instalments already paid. percent_on_track is where it lands if every agreed plan is
    honoured, which stops a plan that is being paid properly from reading as a collection failure.
    """
    from .rtm_payment_plans import plan_progress
    demanded = 0
    collected = 0
    for d in list_demands(limit=5000):
        demanded += int(d.get("amount_pence") or 0)
        collected += int(d.get("amount_pence") or 0) - int(d.get("outstanding_pence") or 0)
    plan_paid = plan_deferred = plan_broken = plans = 0
    for f in list_flats():
        prog = plan_progress(str(f.get("flat_id") or ""))
        if not prog:
            continue
        plans += 1
        plan_paid += int(prog["paid_pence"])
        if prog["on_track"]:
            plan_deferred += int(prog["outstanding_pence"])
        else:
            # A BROKEN plan is not a deferral any more, it is arrears again, and must not sit in the
            # reassuring band. Same rule the chasing cohort uses, so the chart and the mailing cannot
            # disagree about who is in default.
            plan_broken += int(prog["outstanding_pence"])
    unpaid = max(0, demanded - collected - plan_deferred)
    # `unpaid` deliberately still includes broken plans, because every existing caller (the chasing
    # cohort, the overdue list) means "the real arrears" by it and must keep meaning that. But a chart
    # that draws BOTH bands would then count a broken plan twice, so publish the disjoint remainder
    # alongside it: arrears from flats that never agreed anything.
    unpaid_no_plan = max(0, unpaid - plan_broken)
    def pct(n: int) -> float:
        return round(100.0 * n / demanded, 1) if demanded else 0.0
    return {
        "demanded_pence": demanded, "demanded": _money(demanded),
        "collected_pence": collected, "collected": _money(collected),
        "plan_paid_pence": plan_paid, "plan_paid": _money(plan_paid),
        "plan_deferred_pence": plan_deferred, "plan_deferred": _money(plan_deferred),
        "plan_broken_pence": plan_broken, "plan_broken": _money(plan_broken),
        "unpaid_pence": unpaid, "unpaid": _money(unpaid),
        "unpaid_no_plan_pence": unpaid_no_plan, "unpaid_no_plan": _money(unpaid_no_plan),
        "plans": plans,
        "percent_collected": pct(collected),
        "percent_on_track": pct(collected + plan_deferred),
    }


def fund_position() -> dict[str, Any]:
    _ensure_tables()
    refresh_all_demand_statuses(force=False)
    with connect() as con:
        total_demands = con.execute(f"SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_demands WHERE status NOT IN ('Superseded','Cancelled') AND {_REAL_FLATS_ONLY}").fetchone()["s"] or 0
        # payments allocated now spans BOTH real bank allocations and on-account credit applied to demands.
        total_alloc = (con.execute("SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_allocations").fetchone()["s"] or 0) \
                    + (con.execute("SELECT COALESCE(SUM(ca.amount_pence),0) s FROM rtm_mgmt_credit_applications ca "
                                   "JOIN rtm_mgmt_demands d ON d.document_number=ca.document_number "
                                   "WHERE d.status NOT IN ('Superseded','Cancelled')").fetchone()["s"] or 0)
        paid_payables = con.execute("SELECT COALESCE(SUM(paid_amount_pence),0) s FROM rtm_mgmt_payables WHERE status='Paid'").fetchone()["s"] or 0
        committed = con.execute("SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_payables WHERE status IN ('Scheduled','Approved')").fetchone()["s"] or 0
    # ARREARS PER FLAT, FLOORED AT 0, THEN SUMMED. Computing SUM(demands)-SUM(allocations) building-wide
    # let one flat's on-account credit silently cancel another flat's genuine debt, masking real arrears.
    # Computed from demands minus payments (allocations + on-account applied), the SAME source list_demands
    # uses, so it is correct whether or not a demand also wrote a ledger entry.
    with connect() as con:
        dem_by_flat = {r["flat_id"]: int(r["s"] or 0) for r in con.execute(
            f"SELECT flat_id, COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_demands "
            f"WHERE status NOT IN ('Superseded','Cancelled') AND {_REAL_FLATS_ONLY} GROUP BY flat_id").fetchall()}
        paid_by_flat = {r["flat_id"]: int(r["s"] or 0) for r in con.execute(
            "SELECT d.flat_id flat_id, COALESCE(SUM(x.amount_pence),0) s FROM "
            "(SELECT document_number, amount_pence FROM rtm_mgmt_allocations "
            " UNION ALL SELECT document_number, amount_pence FROM rtm_mgmt_credit_applications) x "
            "JOIN rtm_mgmt_demands d ON d.document_number=x.document_number "
            "WHERE d.status NOT IN ('Superseded','Cancelled') GROUP BY d.flat_id").fetchall()}
        # Standing on-account credit (recorded, not yet applied to any demand): a genuine advance for a
        # period not yet demanded. Surfaced separately, never netted against another flat's debt.
        credit_flats = [r["flat_id"] for r in con.execute(
            f"SELECT DISTINCT flat_id FROM rtm_mgmt_ledger_entries "
            f"WHERE kind IN ({','.join('?' for _ in _ON_ACCOUNT_KINDS)}) AND {_REAL_FLATS_ONLY}",
            _ON_ACCOUNT_KINDS).fetchall()]
    arrears_pence = sum(max(0, dem_by_flat.get(f, 0) - paid_by_flat.get(f, 0)) for f in dem_by_flat)
    held_on_account_pence = sum(available_on_account_credit(f) for f in credit_flats)
    total_outstanding = arrears_pence
    # Split the outstanding total by charging cycle. A single blended "arrears" figure is actively
    # misleading the moment a new period is issued: freshly-invoiced charges are NOT arrears, and a
    # director chasing debt needs "what is genuinely overdue" separate from "what we just billed".
    this_period_pence = 0
    brought_forward_pence = 0
    current_period = ""
    try:
        lp = list_periods()
        current_period = lp.get("current") or ""
        for p in lp.get("periods", []):
            if p.get("period") == current_period:
                this_period_pence += int(p.get("outstanding_pence") or 0)
            else:
                brought_forward_pence += int(p.get("outstanding_pence") or 0)
    except Exception:
        this_period_pence = int(total_outstanding)
    return {"demands_raised": _money(total_demands), "payments_allocated": _money(total_alloc),
            "arrears_outstanding": _money(total_outstanding), "arrears_outstanding_pence": total_outstanding,
            "held_on_account": _money(held_on_account_pence), "held_on_account_pence": held_on_account_pence,
            "paid_outgoings": _money(paid_payables),
            "committed_outgoings": _money(committed),
            # Owed in total is arrears_outstanding; these two say WHICH cycle it belongs to.
            "current_period": current_period,
            "this_period_outstanding": _money(this_period_pence),
            "brought_forward_outstanding": _money(brought_forward_pence),
            "brought_forward_is_true_arrears": brought_forward_pence > 0,
            "note": "For bank-cash truth, reconcile this with RTM trust bookkeeper / Metro statements."}


def _normalise_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def _pick(row: dict[str, str], *names: str) -> str:
    for n in names:
        key = _normalise_header(n)
        if key in row and str(row[key]).strip():
            return str(row[key]).strip()
    return ""


def import_metro_csv(path: str | Path) -> dict[str, Any]:
    _ensure_tables()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        raw_rows = [{_normalise_header(k or ""): v for k, v in row.items()} for row in reader]
    imported = 0
    exact = partial = unmatched = duplicates = 0
    txs = []
    for row in raw_rows:
        dt = _pick(row, "date", "transaction date", "posted date", "booking date", "value date") or _today()
        desc = _pick(row, "description", "details", "narrative", "transaction description", "payee", "name", "counterparty", "merchant", "merchant name")
        # Reference survivability: providers scatter the payment reference across many column names
        # (Reference, Payment Reference, Particulars, Analysis Code, Memo, Notes, Type, Category...).
        # Pull it from a WIDE alias set, falling back to the description.
        ref = _pick(row, "reference", "payment reference", "ref", "particulars", "analysis code",
                    "memo", "notes", "remittance", "remittance information", "end to end id",
                    "endtoendid", "your reference", "their reference") or desc
        # Belt-and-braces: also hand the matcher EVERY free-text-ish cell joined together, so a reference
        # sitting in an unexpected column (or split across two) is still seen. Numeric/amount/date/balance
        # cells are excluded so they cannot create spurious token matches.
        _skip = {"amount", "credit", "debit", "paid_in", "paid_out", "money_in", "money_out", "value",
                 "balance", "running_balance", "date", "transaction_date", "posted_date", "booking_date",
                 "value_date"}
        row_blob = " ".join(
            str(v).strip() for k, v in row.items()
            if k not in _skip and str(v).strip() and not _looks_numeric(str(v))
        )
        # "in"/"out" are Metro Bank's own column names (Date, Details, Transaction Type, In, Out, Balance).
        # They are tried LAST so a better-named column always wins; without them Metro exports imported 0 rows.
        amount_s = _pick(row, "amount", "credit", "paid in", "money in", "value", "in")
        debit_s = _pick(row, "debit", "paid out", "money out", "out")
        amount_pence = _money_to_pence(amount_s or debit_s or "0")
        direction = "credit" if amount_pence >= 0 and not debit_s else "debit"
        amount_pence = abs(amount_pence)
        if direction != "credit" or amount_pence <= 0:
            continue
        tx_id = _id("bank")
        try:
            bank_date = _parse_date(dt)
            now = utc_now_iso()
            with connect() as con:
                existing = con.execute(
                    "SELECT id FROM rtm_mgmt_bank_transactions WHERE bank_date=? AND amount_pence=? AND description=? AND reference=? ORDER BY created_at LIMIT 1",
                    (bank_date, amount_pence, desc, ref),
                ).fetchone()
            if existing:
                match = {"match_status": "duplicate", "reason": "Duplicate bank credit fingerprint; manual review required"}
                status = "duplicate"
                duplicate_of = existing["id"]
            else:
                match = _match_bank_credit(ref, desc, amount_pence, extra=row_blob)
                status = match["match_status"]
                duplicate_of = ""
            with connect() as con:
                con.execute(
                    """
                    INSERT INTO rtm_mgmt_bank_transactions(id,bank_date,description,reference,amount_pence,direction,
                        match_status,matched_flat_id,matched_document_number,allocated_pence,duplicate_of,raw_json,source,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (tx_id, bank_date, desc, ref, amount_pence, direction, status,
                     match.get("flat_id"), match.get("document_number"), 0, duplicate_of, json.dumps(row, sort_keys=True),
                     "metro_csv", now, now),
                )
            imported += 1
            if status == "exact":
                exact += 1
                allocate_payment(tx_id, [{"document_number": match["document_number"], "amount_pence": amount_pence}], auto=True)
            elif status == "partial":
                partial += 1
            elif status == "duplicate":
                duplicates += 1
            else:
                unmatched += 1
            txs.append({"id": tx_id, **match, "amount": _money(amount_pence)})
        except Exception as e:
            unmatched += 1
            txs.append({"id": "", "match_status": "unmatched", "reason": str(e), "amount": _money(amount_pence)})
    return {"ok": True, "path": str(path), "imported": imported, "exact": exact,
            "partial": partial, "unmatched": unmatched, "duplicates_or_skipped": duplicates,
            "transactions": txs}


RTM_ACQUISITION_DATE = "2026-07-16"  # RTM constitution/acquisition; bank activity before this is the old regime.


def _is_internal_transfer(description: str, tx_type: str) -> bool:
    """A movement between the RTM's OWN accounts (reserve/service subaccounts), not a leaseholder payment.
    Metro renders these as a TRANSFER whose counterparty is the RTM company itself; they must never be
    read as an inbound service-charge credit."""
    d = (description or "").upper()
    return (f"{_site()['name'].upper()} RTM COMPANY" in d) or ((tx_type or "").upper() == "TRANSFER")


def _confirms_recent_manual_entry(flat_id: str, amount_pence: int, bank_date: str,
                                  window_days: int = 10) -> dict[str, Any] | None:
    """Does this flat have recent MANUAL money-in that this bank credit confirms? Covers a single manual
    entry of the same amount (a full payment, an instalment, an on-account payment) OR a set of them summing
    to it (e.g. two £179.20 instalments confirming one £358.40 bank credit). Returns a small descriptor or
    None. Used so a real bank credit CONFIRMS what was already recorded instead of being re-allocated."""
    from datetime import date as _date, timedelta as _td
    try:
        bd = _date.fromisoformat(str(bank_date)[:10])
    except Exception:
        return None
    lo, hi = (bd - _td(days=window_days)).isoformat(), (bd + _td(days=window_days)).isoformat()
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, amount_pence FROM rtm_mgmt_bank_transactions "
            "WHERE matched_flat_id=? AND source='manual_entry' AND direction='credit' "
            "AND substr(bank_date,1,10) BETWEEN ? AND ? ORDER BY bank_date",
            (flat_id, lo, hi)).fetchall()]
    if not rows:
        return None
    single = next((r for r in rows if int(r["amount_pence"]) == int(amount_pence)), None)
    if single:
        return {"kind": "single", "count": 1, "entries": [single["id"]]}
    if sum(int(r["amount_pence"]) for r in rows) == int(amount_pence):
        return {"kind": "sum", "count": len(rows), "entries": [r["id"] for r in rows]}
    return None


def _allocate_bank_credit_to_flat(txid: str, flat_id: str, amount_pence: int, effective_date: str) -> int:
    """Apply a real bank credit to a flat the way record_payment would: allocate to open demands oldest
    first (partial allowed), then carry any remainder on account. Returns the pence left on account. Used
    to ATTRIBUTE a reference-matched credit that is not yet recorded (no double-count: the metro bank tx is
    the single source of the money)."""
    remaining = int(amount_pence)
    with connect() as con:
        demands = [dict(r) for r in con.execute(
            "SELECT document_number, amount_pence FROM rtm_mgmt_demands WHERE flat_id=? "
            "AND status NOT IN ('Paid','Superseded','Cancelled') ORDER BY due_date, revision",
            (flat_id,)).fetchall()]
    for d in demands:
        if remaining <= 0:
            break
        out = int(d["amount_pence"]) - allocated_to_demand(d["document_number"])
        if out <= 0:
            continue
        amt = min(remaining, out)
        allocate_payment(txid, [{"document_number": d["document_number"], "amount_pence": amt}], auto=True)
        remaining -= amt
    if remaining > 0:
        _ledger_entry(flat_id=flat_id, entry_date=effective_date, kind="on_account",
                      description="Payment on account (from Metro reconciliation)", credit_pence=remaining,
                      document_number="", bank_transaction_id=txid, schedule="on_account",
                      note="Bank credit beyond open demands, carried on account (from Metro reconciliation).")
    return remaining


def _paid_manual_demand_for(flat_id: str, amount_pence: int) -> str | None:
    """A Paid demand (document_number) for this flat whose amount equals amount_pence AND which was settled
    by a manual_entry, so a real bank credit can CONFIRM it rather than double-allocate. None if none."""
    with connect() as con:
        rows = con.execute(
            "SELECT document_number, amount_pence FROM rtm_mgmt_demands WHERE flat_id=? AND status='Paid'",
            (flat_id,)).fetchall()
        for r in rows:
            doc = r["document_number"] if hasattr(r, "keys") else r[0]
            amt = r["amount_pence"] if hasattr(r, "keys") else r[1]
            if int(amt) != int(amount_pence):
                continue
            man = con.execute(
                "SELECT 1 FROM rtm_mgmt_allocations a JOIN rtm_mgmt_bank_transactions b ON b.id=a.bank_transaction_id "
                "WHERE a.document_number=? AND b.source='manual_entry' LIMIT 1", (doc,)).fetchone()
            if man:
                return doc
    return None


def explain_bank_transaction(tx_id: str, note: str, *, by: str = "owner") -> dict[str, Any]:
    """Attach the owner's explanation to a bank line, so an answered question stops being an exception.

    Additive only: it never touches amount, date, direction or match_status, because an explanation must
    not be able to change what the bank said. Clearing is explicit (pass an empty note), so a mistaken
    explanation can be withdrawn and the line returns to the exception list rather than staying silently
    cleared.
    """
    _ensure_tables()
    note = (note or "").strip()
    now = utc_now_iso()
    with connect() as con:
        cur = con.execute(
            "UPDATE rtm_mgmt_bank_transactions SET notes=?, explained_by=?, explained_at=?, updated_at=? "
            "WHERE id=?",
            (note, by if note else "", now if note else "", now, tx_id))
        if not cur.rowcount:
            return {"ok": False, "error": f"no bank transaction {tx_id}"}
        con.commit()
    audit("rtm_bank", "explain", True, payload={"id": tx_id, "cleared": not note})
    return {"ok": True, "id": tx_id, "explained": bool(note)}


def unexplained_bank_lines(*, since: str = RTM_ACQUISITION_DATE, limit: int = 200) -> list[dict[str, Any]]:
    """The reviewer's queue: post-acquisition lines that do not yet tie to something, newest first.

    Deliberately NOT a totals dashboard. A reviewer shown a wall of green numbers reviews nothing, so this
    leads with what is anomalous: money out with no explanation, credits that matched no demand, and
    part-allocated receipts. Anything the owner has explained drops out, so the list shrinks as it is worked.
    """
    _ensure_tables()
    with connect() as con:
        rows = con.execute(
            """SELECT id, bank_date, description, reference, amount_pence, direction, match_status,
                      allocated_pence, duplicate_of, notes
                 FROM rtm_mgmt_bank_transactions
                WHERE bank_date >= ?
                  AND TRIM(COALESCE(notes,'')) = ''
                  AND COALESCE(duplicate_of,'') = ''
                  AND (match_status IN ('expenditure', 'unmatched')
                       OR (direction='credit' AND match_status='partial'))
                ORDER BY bank_date DESC, created_at DESC LIMIT ?""",
            (_parse_date(since), max(1, min(int(limit or 200), 500)))).fetchall()
    return [dict(r) for r in rows]


def _classify_debit(description: str, tx_type: str = "") -> str:
    """A plain-English reason for a payment out, for the reviewer's exception list.

    Deliberately coarse and deliberately honest: it names what the line LOOKS like and says "unclassified"
    when it does not know, rather than inventing a budget category. A wrong category on a real payment is
    worse than an unclassified one, because it silently lands in someone's expenditure analysis. The owner
    supplies the real explanation through the notes field; this only orders the queue of things to explain.
    """
    t = f" {(description or '').lower()} {(tx_type or '').lower()} "
    for needle, reason in (
        ("docmail", "Docmail postage account topup"),
        ("postage", "postage or mailing cost"),
        ("insur", "insurance premium"),
        ("clean", "communal cleaning"),
        ("charge", "bank charge"),
        ("interest", "bank interest"),
        ("invoice", "supplier invoice payment"),
        ("expenses", "expense reimbursement"),
        ("loan", "loan or director balance movement"),
        ("management", "management fee"),
        ("hmrc", "HMRC payment"),
        ("companies house", "Companies House fee"),
    ):
        if needle in t:
            return reason
    return "unclassified payment out"


def _insert_bank_tx(rec: dict[str, Any], status: str, doc: str = "") -> str:
    txid = _id("bank")
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_mgmt_bank_transactions(id,bank_date,description,reference,amount_pence,direction,"
            "match_status,matched_flat_id,matched_document_number,allocated_pence,duplicate_of,raw_json,source,account,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, rec["bank_date"], rec["description"], rec["reference"], rec["amount_pence"],
             # Was hardcoded "credit". The statement carries an Out column and money genuinely leaves the
             # account, so a fixed direction mislabelled every expenditure line it was given.
             rec.get("db_direction") or "credit",
             status, rec.get("flat_id"), doc, 0, "",
             # Was hardcoded "{}". Discarding the source line turned a recoverable import mistake into an
             # unrecoverable one: when the dropped-debits bug surfaced there was nothing left to re-derive
             # from, and only a stray copy of the original export made the loss reversible at all.
             json.dumps(rec.get("raw") or {}, sort_keys=True),
             "metro_csv", rec.get("account") or "current", now, now))
    return txid


# A card payment is often recorded by hand (from the receipt) before the statement that carries it is
# exported. Metro posts card lines one to five working days after the card transaction, and the owner may
# have typed either date, so a manual debit is accepted as the same money when it sits up to this many days
# BEFORE the statement date or this many days AFTER it.
MANUAL_DEBIT_LOOKBACK_DAYS = 10
MANUAL_DEBIT_LOOKAHEAD_DAYS = 3


def _manual_debit_dates(row: dict[str, Any]) -> list[str]:
    """Every date a manual debit could legitimately be matched on: its bank_date plus any transaction or
    posted date recorded alongside it."""
    dates = [str(row.get("bank_date") or "")]
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except Exception:
        raw = {}
    for k in ("transaction_date", "posted_date"):
        if raw.get(k):
            try:
                dates.append(_parse_date(str(raw[k])))
            except Exception:
                pass
    return [d for d in dates if d]


def _match_manual_debits(recs: list[dict[str, Any]], account: str) -> dict[int, dict[str, Any]]:
    """Pair statement debits with manual_entry debits already in the register: same account, same amount,
    date within the posting-lag window. Each manual row is used at most once, the closest date wins, and
    ties go to the oldest entry, so the pairing is deterministic. Returns {index into recs: manual row}."""
    with connect() as con:
        pool = [dict(r) for r in con.execute(
            "SELECT id, bank_date, description, reference, amount_pence, raw_json, notes, created_at "
            "FROM rtm_mgmt_bank_transactions WHERE source='manual_entry' AND direction='debit' "
            "AND COALESCE(account,'current')=? AND COALESCE(duplicate_of,'')='' ORDER BY created_at",
            (account,)).fetchall()]
    used: set[str] = set()
    out: dict[int, dict[str, Any]] = {}
    for i in sorted(range(len(recs)), key=lambda j: (recs[j]["bank_date"], j)):
        rec = recs[i]
        stmt = date.fromisoformat(rec["bank_date"])
        best = None
        for m in pool:
            if m["id"] in used or int(m["amount_pence"]) != int(rec["amount_pence"]):
                continue
            gaps = [(stmt - date.fromisoformat(d)).days for d in _manual_debit_dates(m)]
            ok = [abs(g) for g in gaps if -MANUAL_DEBIT_LOOKAHEAD_DAYS <= g <= MANUAL_DEBIT_LOOKBACK_DAYS]
            if not ok:
                continue
            key = (min(ok), m["created_at"])
            if best is None or key < best[0]:
                best = (key, m)
        if best:
            used.add(best[1]["id"])
            out[i] = best[1]
    return out


def _adopt_manual_debit(manual: dict[str, Any], rec: dict[str, Any]) -> None:
    """The statement line confirms a manual debit: turn that row INTO the statement line (Metro date, text
    and raw line, source metro_csv) instead of inserting a second copy of the same money. Its mapping,
    payable and evidence stay attached. The manual values are kept inside raw_json for the audit trail, and
    because the row is now metro_csv, re-importing the statement fingerprint-skips it like any other line."""
    raw = dict(rec.get("raw") or {})
    try:
        manual_raw = json.loads(manual.get("raw_json") or "{}")
    except Exception:
        manual_raw = {}
    raw["_manual_entry"] = {"bank_date": manual["bank_date"], "description": manual["description"],
                            "reference": manual["reference"], "raw": manual_raw}
    now = utc_now_iso()
    note = (str(manual.get("notes") or "").strip()
            + f" Confirmed by Metro statement line {rec['bank_date']} '{rec['description']}' on {now[:10]}.").strip()
    with connect() as con:
        con.execute(
            "UPDATE rtm_mgmt_bank_transactions SET bank_date=?, description=?, reference=?, raw_json=?, "
            "source='metro_csv', notes=?, updated_at=? WHERE id=? AND source='manual_entry'",
            (rec["bank_date"], rec["description"], rec["reference"], json.dumps(raw, sort_keys=True),
             note, now, manual["id"]))


def record_manual_expenditure(*, amount: Any, bank_date: str, supplier: str, description: str,
                              schedule: str = "sch1", budget_line: str = "Sundries",
                              transaction_date: str | None = None, invoice_date: str | None = None,
                              reference: str = "", document_ids: list[int] | tuple[int, ...] = (),
                              notes: str = "", account: str = "current") -> dict[str, Any]:
    """Record a payment out by hand, ahead of the statement that will carry it: a Paid payable under the
    budget line (so budget-vs-actual sees it now), a manual_entry debit in the bank register linked to it,
    and evidence links to the filed documents. When the Metro CSV holding the real line is imported, the
    importer ADOPTS this row (see _match_manual_debits) rather than adding the money a second time.
    bank_date is the date the bank shows; transaction_date is the card date when it differs."""
    _ensure_tables()
    pence = _money_to_pence(amount) if amount not in (None, "") else 0
    if pence <= 0:
        return {"ok": False, "error": "amount must be positive"}
    if account != "current":
        return {"ok": False, "error": "manual expenditure belongs to the current account"}
    bd = _parse_date(bank_date)
    td = _parse_date(transaction_date) if transaction_date else bd
    pay = add_payable(supplier=supplier, description=description, schedule=schedule, amount=_money(pence),
                      invoice_date=invoice_date or td, budget_line=budget_line,
                      notes=("Manual entry. " + notes).strip())
    pid = pay["id"]
    mark_payable_paid(pid, amount=_money(pence), paid_date=bd, bank_reference=(reference or supplier)[:190])
    txid = _id("bank")
    now = utc_now_iso()
    raw = {"manual": True, "transaction_date": td, "posted_date": bd, "out": f"{pence / 100:.2f}",
           "rtm_document_ids": [int(d) for d in document_ids]}
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_mgmt_bank_transactions(id,bank_date,description,reference,amount_pence,direction,"
            "match_status,matched_flat_id,matched_document_number,allocated_pence,duplicate_of,raw_json,source,"
            "account,notes,explained_by,explained_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (txid, bd, f"{supplier} (manual entry)", reference, pence, "debit", "expenditure_mapped", None, pid,
             0, "", json.dumps(raw, sort_keys=True), "manual_entry", account,
             ("Entered manually ahead of the Metro statement. " + notes).strip(), "owner", now, now, now))
    from .rtm_docs import link_document_to_payable
    for d in document_ids:
        link_document_to_payable(int(d), pid, notes=f"Evidence for {supplier} {_money(pence)}")
    return {"ok": True, "bank_txn_id": txid, "payable_id": pid, "amount": _money(pence), "bank_date": bd}


def reconcile_metro_csv(path: str | Path, *, since: str = RTM_ACQUISITION_DATE, apply: bool = False,
                        store_document: bool = True, account: str = "current") -> dict[str, Any]:
    """SAFE reconciliation of a Metro Bank CSV against the RTM ledger. DRY-RUN by default (apply=False):
    classify every credit and write NOTHING. Never moves money, it only records what already happened.

    Per-credit categories:
      internal_transfer, RTM-to-RTM subaccount movement (reserve/service). Skipped, never a payment.
      pre_acquisition, dated before `since`. Recorded for audit only, never auto-allocated.
      allocate, matches a flat with an OPEN demand of the exact outstanding amount: a real
                          leaseholder payment, so allocate it (bank-sourced).
      confirms_manual, matches a flat whose demand is ALREADY Paid via a manual entry: record the real
                          bank credit as CONFIRMING evidence, do NOT add a second allocation (the exact
                          double-count the naive importer would have caused).
      unmatched, no reference/name hit or amount mismatch: lands in the review queue.

    apply=True performs those writes and (store_document) files the CSV itself as bank-statement evidence.
    """
    _ensure_tables()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    since_d = _parse_date(since)
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = [{_normalise_header(k or ""): v for k, v in row.items()} for row in csv.DictReader(f)]
    # Precompute the distinct reserve/service split amounts across live flats, so a TRANSFER of exactly
    # a reserve slice (e.g. 145.23) or a service slice (929.93) can be tagged to the right subaccount ,
    # the raw material for the trust-money segregation audit.
    reserve_amounts, service_amounts = _fund_split_amounts()
    cats: dict[str, list] = {"internal_transfer": [], "pre_acquisition": [], "allocate": [],
                             "confirms_manual": [], "attribute": [], "unmatched": [], "expenditure": [],
                             "interest": []}
    for row in rows:
        dt = _pick(row, "date", "transaction date", "posted date", "booking date", "value date") or _today()
        desc = _pick(row, "description", "details", "narrative", "payee", "name", "counterparty")
        tx_type = _pick(row, "transaction type", "type")
        ref = _pick(row, "reference", "payment reference", "ref", "particulars", "memo", "notes") or desc
        credit_s = _pick(row, "amount", "credit", "paid in", "money in", "value", "in")
        debit_s = _pick(row, "debit", "paid out", "money out", "out")
        is_transfer = _is_internal_transfer(desc, tx_type)
        # FUND ACCOUNTS (reserve / service): these interest-bearing accounts only ever receive sweeps from
        # the current account, send money back to it, or accrue interest. NOTHING here is a leaseholder
        # receipt or a supplier payment (those live in the current account), so a fund line is classified as
        # an internal transfer or interest and logged for the internal fund reconciliation ONLY, never
        # matched to a demand, a flat, or a budget line. This is the whole reason a fund line can never
        # pollute collection% or budget-vs-actual: it takes this branch and returns before any of that.
        if account != "current":
            amt = abs(_money_to_pence(credit_s or debit_s or "0"))
            if amt <= 0:
                continue
            try:
                _bd = _parse_date(dt)
            except Exception:
                _bd = _today()
            _in = bool(credit_s) and not debit_s
            rec = {"bank_date": _bd, "description": desc, "reference": ref, "amount_pence": amt,
                   "amount": _money(amt), "flat_id": None, "account": account, "raw": dict(row),
                   "db_direction": "credit" if _in else "debit"}
            if "interest" in (tx_type or "").lower() or "interest" in (desc or "").lower():
                rec["reason"] = f"interest credited to the {account} fund"
                cats["interest"].append(rec); continue
            if is_transfer:
                # Status stays 'internal_transfer' (NOT reserve_movement/service_movement, which tag a sweep
                # OUT of the current account INTO a subaccount). Here the account column already says which
                # fund this is, so a fund-tag would only mislabel the status and hide the row from the fund
                # reconciliation.
                rec["reason"] = f"internal transfer {'into' if _in else 'out of'} the {account} fund"
                cats["internal_transfer"].append(rec); continue
            # No leaseholder pays a fund directly and no supplier is paid from it: flag anything else.
            rec["reason"] = f"unexpected non-transfer line in the {account} fund account, needs review"
            cats["unmatched"].append(rec); continue
        # A TRANSFER between the RTM's own accounts is a subaccount MOVEMENT and must be captured in EITHER
        # direction, a sweep INTO a subaccount shows as a debit OUT of the main account.
        #
        # EXPENDITURE used to be dropped here outright ("continue"), on the reasoning that a debit is not a
        # leaseholder credit and this routine exists to confirm leaseholder payments. That was true of the
        # routine and false of the database it writes to: the register became the only record of the bank,
        # so discarding debits meant the company's entire spending side did not exist, while the import
        # reported success because the credit matching genuinely worked. Any fund position derived from it
        # would have shown income with no outgoings.
        is_debit = False
        if is_transfer:
            amount_pence = abs(_money_to_pence(credit_s or debit_s or "0"))
            transfer_dir = "in" if credit_s else "out"
        elif debit_s and not credit_s:
            amount_pence = abs(_money_to_pence(debit_s))
            is_debit = True
        else:
            amount_pence = abs(_money_to_pence(credit_s or "0"))
        if amount_pence <= 0:
            continue
        try:
            bank_date = _parse_date(dt)
        except Exception:
            bank_date = _today()
        blob = " ".join(
            str(v).strip() for k, v in row.items()
            if k not in {"amount", "credit", "debit", "paid_in", "paid_out", "money_in", "money_out",
                         "in", "out", "value", "balance", "date", "transaction_date"}
            and str(v).strip() and not _looks_numeric(str(v)))
        rec = {"bank_date": bank_date, "description": desc, "reference": ref,
               "amount_pence": amount_pence, "amount": _money(amount_pence), "flat_id": None,
               "account": account,
               # The whole source line, so every stored row traces back to the statement it came from and
               # can be re-derived or re-classified without another export. Includes the running Balance,
               # which is the bank's own figure and therefore the independent side of any reconciliation.
               "raw": dict(row)}
        # EXPENDITURE: money out that is not an internal sweep. Recorded, never matched to a demand (a
        # payment out cannot settle a leaseholder's charge), and gated by the acquisition date exactly like
        # credits, so the previous regime's spending stays out of this company's books.
        if is_debit:
            rec["db_direction"] = "debit"
            if bank_date < since_d:
                rec["reason"] = f"pre-acquisition payment out ({since})"
                cats["pre_acquisition"].append(rec); continue
            rec["reason"] = _classify_debit(desc, tx_type)
            cats["expenditure"].append(rec); continue
        # TRANSFERS (subaccount movements): a pre-acquisition sweep is the old regime and is gated out; a
        # post-acquisition OUT sweep of a reserve/service slice is tagged to its fund for the trust audit.
        if is_transfer:
            if bank_date < since_d:
                rec["reason"] = f"pre-acquisition transfer ({since})"
                rec["db_direction"] = "credit" if transfer_dir == "in" else "debit"
                cats["pre_acquisition"].append(rec); continue
            rec["direction"] = transfer_dir
            # An OUT sweep is money LEAVING this account. Without this it was stored as a credit, so the
            # register showed a sweep as incoming twice over: once here and once in the receiving account.
            rec["db_direction"] = "credit" if transfer_dir == "in" else "debit"
            if transfer_dir == "out" and amount_pence in reserve_amounts:
                rec["fund"] = "reserve"; rec["reason"] = "sweep into the RESERVE fund subaccount"
            elif transfer_dir == "out" and amount_pence in service_amounts:
                rec["fund"] = "service"; rec["reason"] = "sweep into the SERVICE charge subaccount"
            else:
                rec["fund"] = None; rec["reason"] = f"internal RTM transfer ({transfer_dir})"
            cats["internal_transfer"].append(rec); continue
        # CREDITS (leaseholder payments): match REGARDLESS of date. A VALID pre-acquisition payment is a
        # real credit, but settled EFFECTIVE the acquisition date (not the earlier bank date) , the owner's rule:
        # "valid pre-acquisition payments are counted as credits and settled effective the acquisition date".
        pre = bank_date < since_d
        rec["effective_date"] = since if pre else bank_date
        m = _match_bank_credit(ref, desc, amount_pence, extra=blob)
        rec["flat_id"] = m.get("flat_id")
        if m["match_status"] == "exact":
            rec["document_number"] = m["document_number"]
            rec["reason"] = m.get("reason", "exact") + (f", pre-acquisition payment, settled effective {since}" if pre else "")
            cats["allocate"].append(rec); continue
        if m.get("flat_id"):
            # 1) Does this credit CONFIRM money already recorded by hand for the flat (single entry, or a set
            #    summing to it: instalments, on-account, part-payments)? If so, record it as evidence only,
            #    never a second allocation.
            conf = _confirms_recent_manual_entry(m["flat_id"], amount_pence, bank_date)
            if conf:
                rec["confirms"] = conf
                rec["reason"] = (f"confirms {conf['count']} manually-recorded payment(s) for this flat "
                                 f"({conf['kind']} match); not re-allocated")
                cats["confirms_manual"].append(rec); continue
            # Belt and braces: the original narrow check (an already-Paid demand of the exact amount).
            paid_doc = _paid_manual_demand_for(m["flat_id"], amount_pence)
            if paid_doc:
                rec["document_number"] = paid_doc
                rec["reason"] = "confirms a manually-recorded payment (already Paid); not re-allocated"
                cats["confirms_manual"].append(rec); continue
            # 2) Reference-matched to a flat but NOT already recorded: it is that flat's money. ATTRIBUTE it,
            #    allocating to open demands and carrying any excess on account (record_payment's own rule).
            #    A pre-acquisition credit is NOT auto-attributed: it stays in the pre_acquisition review
            #    bucket (the owner's rule: pre-acquisition payments are reviewed, settled effective the acquisition
            #    date only when confirmed valid).
            if not pre:
                rec["reason"] = (m.get("reason", "reference match")
                                 + "; attributed to flat, will allocate and carry any excess on account")
                cats["attribute"].append(rec); continue
        if pre:
            rec["reason"] = ("pre-acquisition credit with no automatic match: review whether it is a valid "
                             f"current payment (allocate, effective {since}) or old regime")
            cats["pre_acquisition"].append(rec); continue
        rec["reason"] = m.get("reason", "no match")
        cats["unmatched"].append(rec)

    # MANUAL DEBITS. A payment out entered by hand before this statement existed is the same money as its
    # statement line. Flag the pairing here so the dry run shows it; the apply step re-derives it after the
    # re-import guard, so a line already on file can never claim a manual entry.
    if account == "current":
        for i, man in _match_manual_debits(cats["expenditure"], account).items():
            rec = cats["expenditure"][i]
            rec["confirms_manual_debit"] = man["id"]
            rec["reason"] = (f"{rec.get('reason', '')}; confirms manual entry {man['id']} of {rec['amount']} "
                             f"({man['bank_date']}), adopted, not added again").lstrip("; ")

    summary = {k: len(v) for k, v in cats.items()}
    result = {"ok": True, "path": str(path), "since": since, "apply": apply,
              "summary": summary, "categories": cats}
    if not apply:
        result["dry_run"] = True
        return result

    # RE-IMPORT GUARD. This path had no row-level duplicate check: importing the same statement twice added
    # every line again (40 -> 67 -> 94 rows on a real file, none flagged). That was survivable while the
    # register only evidenced payment confirmations, because allocation was separately protected and the
    # money figures never moved. It stops being survivable the moment the register becomes the source of the
    # fund position, which is what deriving the book from it does.
    #
    # Counted, not set-based, because a single statement legitimately repeats identical lines: two £145.23
    # reserve sweeps and two £929.93 service sweeps on the same day are two real movements, one per block.
    # So the rule is "the register should hold as many copies as the statement shows, and no more": insert
    # only the shortfall, which makes a re-import a no-op and a genuine repeat still land.
    def _fp(rec: dict[str, Any]) -> tuple:
        return (rec["bank_date"], int(rec["amount_pence"]), rec.get("description") or "",
                rec.get("reference") or "", rec.get("db_direction") or "credit")

    from collections import Counter
    with connect() as con:
        have = Counter(
            (r["bank_date"], int(r["amount_pence"]), r["description"] or "", r["reference"] or "",
             r["direction"] or "credit")
            for r in con.execute("SELECT bank_date, amount_pence, description, reference, direction "
                                 "FROM rtm_mgmt_bank_transactions WHERE source='metro_csv' AND account=?",
                                 (account,)).fetchall())
    skipped = 0
    for _bucket in ("allocate", "confirms_manual", "attribute", "unmatched", "internal_transfer",
                    "pre_acquisition", "expenditure", "interest"):
        for rec in cats[_bucket]:
            fp = _fp(rec)
            if have[fp] > 0:
                have[fp] -= 1
                rec["_already_recorded"] = True
                skipped += 1

    applied = {"allocated": 0, "confirmed": 0, "recorded_unmatched": 0,
               "skipped_already_recorded": skipped}
    for rec in cats["allocate"]:
        if rec.get("_already_recorded"):
            continue
        # Settle a valid pre-acquisition payment effective the acquisition date: the bank tx (and thus the
        # ledger entry allocate_payment writes) carries effective_date, not the earlier actual bank date.
        eff = rec.get("effective_date") or rec["bank_date"]
        txid = _insert_bank_tx({**rec, "bank_date": eff}, "exact", rec["document_number"])
        allocate_payment(txid, [{"document_number": rec["document_number"], "amount_pence": rec["amount_pence"]}], auto=True)
        applied["allocated"] += 1
    for rec in cats["confirms_manual"]:
        if rec.get("_already_recorded"):
            continue
        doc = rec.get("document_number") or ""
        _insert_bank_tx(rec, "confirms_manual", doc)
        # Evidence attaches to the demand when we know it, else to the flat (on-account / instalment).
        if doc:
            add_evidence(target_type="demand", target_id=doc, document_kind="bank_confirmation",
                         title=f"Metro credit {rec['amount']} on {rec['bank_date']}",
                         notes=(f"Real Metro bank credit of {rec['amount']} confirming the recorded payment "
                                f"for {doc}. Not re-allocated."))
        else:
            add_evidence(target_type="flat", target_id=rec.get("flat_id") or "", document_kind="bank_confirmation",
                         title=f"Metro credit {rec['amount']} on {rec['bank_date']}",
                         notes=(f"Real Metro bank credit of {rec['amount']} confirming a recorded payment "
                                f"(instalment/on-account) for {rec.get('flat_id')}. Not re-allocated."))
        applied["confirmed"] += 1
    # ATTRIBUTE: a reference-matched credit not yet recorded. Insert the bank line and apply it like a
    # payment (allocate to open demands, remainder on account). The metro bank tx is the single money source.
    for rec in cats["attribute"]:
        if rec.get("_already_recorded"):
            continue
        eff = rec.get("effective_date") or rec["bank_date"]
        txid = _insert_bank_tx({**rec, "bank_date": eff}, "reviewed", "")
        on_account = _allocate_bank_credit_to_flat(txid, rec["flat_id"], rec["amount_pence"], eff)
        applied["attributed"] = applied.get("attributed", 0) + 1
        if on_account > 0:
            applied["attributed_on_account"] = applied.get("attributed_on_account", 0) + on_account
    for rec in cats["unmatched"]:
        if rec.get("_already_recorded"):
            continue
        _insert_bank_tx(rec, "unmatched")
        applied["recorded_unmatched"] += 1
    # Record the non-ledger rows too (transfers + pre-acquisition) so the bank data is COMPLETE and
    # auditable, with an honest status. These never allocate, so they cannot pollute money figures.
    for rec in cats["internal_transfer"]:
        if rec.get("_already_recorded"):
            continue
        status = {"reserve": "reserve_movement", "service": "service_movement"}.get(rec.get("fund"), "internal_transfer")
        _insert_bank_tx(rec, status)
    for rec in cats["pre_acquisition"]:
        if rec.get("_already_recorded"):
            continue
        _insert_bank_tx(rec, "pre_acquisition")
    # EXPENDITURE: recorded as its own status so it is unmistakably money out and can never be read as a
    # leaseholder receipt. Never allocated: a payment out cannot settle a demand.
    # A line that confirms a manual debit ADOPTS that row instead of inserting a second copy of the money.
    fresh = [rec for rec in cats["expenditure"] if not rec.get("_already_recorded")]
    adopt = _match_manual_debits(fresh, account) if account == "current" else {}
    applied["adopted_manual_debits"] = 0
    for i, rec in enumerate(fresh):
        if i in adopt:
            _adopt_manual_debit(adopt[i], rec)
            applied["adopted_manual_debits"] += 1
            continue
        _insert_bank_tx(rec, "expenditure")
    # INTEREST: money the reserve/service fund earned. Logged (it grows the fund balance) but it is not a
    # leaseholder charge and not a budget line, so it never touches collection% or budget-vs-actual.
    _interest_recorded = 0
    for rec in cats["interest"]:
        if rec.get("_already_recorded"):
            continue
        _insert_bank_tx(rec, "interest")
        _interest_recorded += 1
    applied["recorded_expenditure"] = len(fresh) - applied["adopted_manual_debits"]
    applied["recorded_interest"] = _interest_recorded
    applied["recorded_audit_only"] = len(cats["internal_transfer"]) + len(cats["pre_acquisition"])
    if store_document:
        try:
            add_evidence(target_type="bank_statement", target_id=path.name, document_kind="bank_statement",
                         title=f"Metro statement {path.name}", path=str(path),
                         notes=f"Reconciled: {summary}; applied {applied}")
        except Exception:
            pass
    # the owner's rule end-to-end: after confirming/allocating, make sure NO settlement is still dated before the
    # acquisition date. This catches payments that were manually marked Paid earlier with their real (pre-
    # acquisition) receipt date , they are valid credits, so they settle effective the acquisition date.
    # Only the current (operating) account carries leaseholder settlements to re-date; a fund account makes
    # no allocations, so there is nothing to re-settle and no reason to sweep the whole ledger.
    if account == "current":
        swept = settle_effective_acquisition(since)
        applied["resettled_effective_acquisition"] = swept["corrected"]
    result["applied"] = applied
    return result


def _fund_split_amounts() -> tuple[set[int], set[int]]:
    """The distinct reserve-slice and service-slice pence amounts across live flats, so a bank TRANSFER of
    exactly one of them can be tagged to the reserve or service subaccount. Flats in the same band share a
    split, so this is a handful of values."""
    reserve: set[int] = set()
    service: set[int] = set()
    try:
        from .rtm_trust import subaccount_split
        with connect() as con:
            flats = [r[0] for r in con.execute(
                "SELECT DISTINCT flat_id FROM rtm_mgmt_demands WHERE status NOT IN ('Superseded','Cancelled')").fetchall()]
        for fid in flats:
            try:
                sp = subaccount_split(fid)
                reserve.add(int(sp["reserve_pence"]))
                service.add(int(sp["service_pence"]))
            except Exception:
                continue
    except Exception:
        pass
    return reserve, service


def set_fund_opening(fund: str, amount_pence: int, as_of: str = RTM_ACQUISITION_DATE) -> dict[str, Any]:
    """Record a fund's OPENING CAPITAL (money brought over at handover, e.g. the Harbour reserve). Stored as
    metadata, NOT a bank line: the money itself arrives as the real transfers in the statement, so this only
    labels how much of the fund balance is capital vs accrued. Used by fund_reconciliation for the split."""
    fund = fund.strip().lower()
    set_app_state(f"rtm_fund_opening_{fund}", json.dumps({"amount_pence": int(amount_pence), "as_of": as_of[:10]}))
    return {"ok": True, "fund": fund, "amount_pence": int(amount_pence), "as_of": as_of[:10]}


def _fund_opening_pence(fund: str) -> int:
    raw = get_app_state(f"rtm_fund_opening_{fund.strip().lower()}", "") or ""
    if not raw:
        return 0
    try:
        return int(json.loads(raw).get("amount_pence", 0))
    except Exception:
        return 0


def _bank_stated_balance(rows: list[dict[str, Any]]) -> int | None:
    """The bank's own running Balance from the latest ingested row: the independent side of the fund
    reconciliation. Parsed from the stored raw statement line so it is the bank's figure, not ours."""
    if not rows:
        return None
    try:
        raw = json.loads(rows[-1].get("raw_json") or "{}")
    except Exception:
        return None
    for k, v in raw.items():
        if str(k).strip().lower() == "balance" and str(v).strip():
            return _money_to_pence(str(v))
    return None


def fund_reconciliation() -> dict[str, Any]:
    """Internal reconciliation of the two interest-bearing fund accounts (reserve, service). Neither ever
    touches budget or collection: every line is an internal transfer or interest. For each fund this proves
    the ingested movements reconcile to the bank's OWN stated closing balance (so nothing was lost in
    ingestion), and shows the composition: opening/handover capital vs interest earned vs the net
    service-charge slices swept in. A non-zero difference means a statement gap to chase, never forced to 0."""
    _ensure_tables()
    funds: dict[str, Any] = {}
    for fund in ("reserve", "service"):
        with connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT bank_date, amount_pence, direction, match_status, raw_json "
                "FROM rtm_mgmt_bank_transactions WHERE account=? ORDER BY bank_date, created_at",
                (fund,)).fetchall()]
        tin = sum(int(r["amount_pence"]) for r in rows
                  if r["match_status"] == "internal_transfer" and r["direction"] == "credit")
        tout = sum(int(r["amount_pence"]) for r in rows
                   if r["match_status"] == "internal_transfer" and r["direction"] == "debit")
        interest = sum(int(r["amount_pence"]) for r in rows if r["match_status"] == "interest")
        computed = tin + interest - tout  # from a zero start: the full ingested history should sum to balance
        opening = _fund_opening_pence(fund)
        stated = _bank_stated_balance(rows)
        diff = (stated - computed) if stated is not None else None
        funds[fund] = {
            "rows": len(rows),
            "transfers_in": _money(tin), "transfers_out": _money(tout),
            "interest_earned": _money(interest), "interest_pence": interest,
            "opening_capital": _money(opening), "opening_capital_pence": opening,
            "computed_balance": _money(computed), "computed_balance_pence": computed,
            "bank_stated_balance": (_money(stated) if stated is not None else None),
            "difference": (_money(diff) if diff is not None else None),
            "reconciled": (diff == 0) if diff is not None else None,
            "accrued_since_opening": _money(max(0, computed - opening)),
        }
    return {"ok": True, "funds": funds,
            "note": "Fund accounts are internal: interest + inter-account transfers only, never budget or "
                    "collection. 'reconciled' means the ingested movements match the bank's own closing balance."}


def subaccount_audit(year: str = "2027") -> dict[str, Any]:
    """Trust-money SEGREGATION audit. For every live demand, split it into the reserve fund and the
    service-charge fund via the canonical engine, weighted by how much has actually been PAID; then compare
    against how much was really MOVED into each subaccount at the bank (reconciled Metro transfers tagged
    reserve_movement/service_movement). The reserve 'expected' and 'bank moved' should agree, that is the
    proof the reserve money was properly segregated, which is exactly what an auditor or the FTT asks for.
    """
    _ensure_tables()
    from .rtm_trust import subaccount_split
    reserve_expected = service_expected = 0
    reserve_billed = service_billed = 0
    with connect() as con:
        con.row_factory = __import__("sqlite3").Row
        demands = [dict(r) for r in con.execute(
            "SELECT document_number, flat_id, amount_pence FROM rtm_mgmt_demands "
            "WHERE status NOT IN ('Superseded','Cancelled')").fetchall()]
    for d in demands:
        try:
            sp = subaccount_split(d["flat_id"], year=year)
        except Exception:
            continue
        r, s, tot = int(sp["reserve_pence"]), int(sp["service_pence"]), int(sp["total_pence"])
        amt = int(d["amount_pence"])
        if tot > 0 and amt != tot:            # scale the canonical split to THIS demand (arrears/revisions)
            r = round(r * amt / tot); s = amt - r
        reserve_billed += r; service_billed += s
        paid = allocated_to_demand(d["document_number"])
        frac = min(1.0, paid / amt) if amt > 0 else 0.0
        reserve_expected += round(r * frac); service_expected += round(s * frac)
    bank_reserve = bank_service = 0
    with connect() as con:
        con.row_factory = __import__("sqlite3").Row
        for t in con.execute("SELECT amount_pence, match_status FROM rtm_mgmt_bank_transactions "
                             "WHERE match_status IN ('reserve_movement','service_movement')").fetchall():
            if t["match_status"] == "reserve_movement":
                bank_reserve += int(t["amount_pence"])
            else:
                bank_service += int(t["amount_pence"])

    def _fund(expected: int, moved: int) -> dict[str, Any]:
        return {"expected_from_payments": _money(expected), "expected_pence": expected,
                "bank_moved_to_subaccount": _money(moved), "bank_moved_pence": moved,
                "difference": _money(expected - moved), "difference_pence": expected - moved,
                "reconciled": (moved == expected)}

    return {"ok": True, "year": year,
            "reserve_fund": _fund(reserve_expected, bank_reserve),
            "service_fund": _fund(service_expected, bank_service),
            "reserve_billed_total": _money(reserve_billed),
            "service_billed_total": _money(service_billed),
            "note": ("'expected_from_payments' is the reserve/service slice of what has actually been PAID. "
                     "'bank_moved_to_subaccount' is the total of reconciled Metro transfers into that "
                     "subaccount. Run reconcile-metro (apply) first so the bank movements are captured; then "
                     "the two should match, proving the reserve money was segregated at the bank.")}


def settle_effective_acquisition(since: str = RTM_ACQUISITION_DATE) -> dict[str, Any]:
    """Enforce the owner's rule: a VALID payment received before the acquisition date is a credit that
    settles EFFECTIVE the acquisition date. Any payment ledger entry currently dated before `since`
    is re-dated to `since`, keeping the true receipt date in an audit note (correction, not erasure).
    Idempotent: only touches entries still dated before `since`. Balances are untouched (amounts do
    not change), only the effective settlement date moves forward to acquisition."""
    _ensure_tables()
    changed: list[dict[str, Any]] = []
    with connect() as con:
        con.row_factory = __import__("sqlite3").Row
        rows = con.execute(
            "SELECT id, flat_id, document_number, entry_date, immutable_note FROM rtm_mgmt_ledger_entries "
            "WHERE kind = 'payment' AND entry_date < ?", (since,)).fetchall()
        for r in rows:
            note = (r["immutable_note"] or "").strip()
            stamp = f"settled effective acquisition {since} (payment received {r['entry_date']})"
            note = f"{note}; {stamp}" if note else stamp
            con.execute("UPDATE rtm_mgmt_ledger_entries SET entry_date = ?, immutable_note = ? WHERE id = ?",
                        (since, note, r["id"]))
            changed.append({"flat_id": r["flat_id"], "document_number": r["document_number"],
                            "received": r["entry_date"], "settled_effective": since})
        con.commit()
    return {"ok": True, "acquisition_date": since, "corrected": len(changed), "entries": changed}


def _canon_ref(s: str) -> str:
    """Canonical form of a payment reference for matching: keep only letters and
    digits, uppercase. Collapses every way a payer or bank might render a name with
    separators or punctuation to ONE comparable string:
        'DE VERMEULEN' / 'DE-VERMEULEN' / 'DEVERMEULEN'  -> 'DEVERMEULEN'
        \"O'BRIEN\" / 'O BRIEN' / 'OBRIEN'              -> 'OBRIEN'
    This matters because the UK Open Banking reference field is free-text and capped
    at 18 chars, so spacing/punctuation/truncation are the norm, not the exception."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _alpha_tokens(s: str) -> list[str]:
    """Alphabetic tokens (length >= 2) from a raw string, lowercased. Used for a
    conservative token-subset fallback so a stored ref like 'DEVERMEULEN' can still be
    recognised when the payer typed only the distinctive part ('VERMEULEN')."""
    return [t for t in re.findall(r"[a-z]+", (s or "").lower()) if len(t) >= 2]


# Generic name particles that are NOT distinctive enough to match on alone. A bank
# string of just 'DE' or 'VAN' must never pull in De Vermeulen or Van Someone.
_REF_STOPWORDS = {"de", "van", "von", "der", "den", "le", "la", "du", "di", "da",
                  "el", "al", "mc", "mac", "st", "the", "and", "flat", "ref"}


def _distinctive_tokens(s: str) -> set[str]:
    return {t for t in _alpha_tokens(s) if t not in _REF_STOPWORDS}


def _looks_numeric(s: str) -> bool:
    """True if a cell is purely a number/amount/date-ish value (so it is excluded from the free-text
    blob fed to the matcher, numbers must not create spurious token or substring matches)."""
    t = (s or "").strip()
    if not t:
        return False
    # money/amount/balance, dates, plain integers: digits with optional separators and one sign.
    return bool(re.fullmatch(r"[-+]?[\d.,/£$€\s]+", t))


def _match_bank_credit(reference: str, description: str, amount_pence: int, extra: str = "") -> dict[str, Any]:
    # The matcher searches reference + description + any extra free-text columns combined, so a payment
    # reference is found wherever the provider's export put it (Reference, Particulars, Memo, an
    # unexpected column, or split across several).
    raw_needle = f"{reference} {description} {extra}".strip()
    canon_needle = _canon_ref(raw_needle)
    needle_distinct = _distinctive_tokens(raw_needle)
    with connect() as con:
        flats = [dict(r) for r in con.execute("SELECT * FROM rtm_mgmt_flats WHERE payment_reference <> ''").fetchall()]
        # Pass 1: canonical containment, BOTH directions. Forward (stored ref appears in the bank
        # string) is the normal case. Reverse (a token from the bank string , possibly OB-truncated to
        # 18 chars , appears within the stored ref) rescues a payment that arrived with only a leading
        # fragment of a longer stored reference. Reverse tests each canonical TOKEN of the needle (not
        # the whole concatenated blob, which can repeat fragments) and requires a fragment of >=5
        # canonical chars so a tiny accidental substring cannot match.
        needle_canon_tokens = {_canon_ref(t) for t in re.split(r"\s+", raw_needle) if len(_canon_ref(t)) >= 5}
        candidates = []
        for fl in flats:
            cref = _canon_ref(fl.get("payment_reference", ""))
            if not cref:
                continue
            if cref in canon_needle:
                candidates.append(fl)
            elif any(tok in cref for tok in needle_canon_tokens):
                candidates.append(fl)
        match_basis = "canonical reference"
        # Pass 2 (fallback): only if pass 1 found nothing. Recognise a stored ref by its DISTINCTIVE
        # tokens appearing in the bank string. Two sub-rules:
        #   (a) ALL distinctive tokens present (the original, strict rule), OR
        #   (b) if the stored ref has >=2 distinctive tokens, tolerate ONE missing (spacing/OCR/partial
        #       reference) , still conservative because >=2 distinctive tokens must hit, and a stored ref
        #       with a single distinctive token never falls through to (b).
        if not candidates:
            for fl in flats:
                stored_distinct = _distinctive_tokens(fl.get("payment_reference", ""))
                if not stored_distinct:
                    continue
                present = stored_distinct & needle_distinct
                if stored_distinct <= needle_distinct:
                    candidates.append(fl)
                elif len(stored_distinct) >= 2 and len(present) >= max(2, len(stored_distinct) - 1):
                    candidates.append(fl)
            if candidates:
                match_basis = "distinctive name tokens (no exact reference)"
        if not candidates:
            # Pass 3 (payer alias): a company or third party paying on a leaseholder's behalf (e.g. Example
            # Holdings Limited paying a flat) may send with a payer NAME recorded in payer_aliases and an
            # imperfect or absent reference. Match on the alias appearing in the bank string; the amount
            # still has to clear an open demand exactly (checked below), so an alias never grabs a payment
            # on name alone.
            import json as _json
            for fl in flats:
                try:
                    aliases = _json.loads(fl.get("payer_aliases") or "[]")
                except Exception:
                    aliases = []
                if not isinstance(aliases, list):
                    continue
                if any(_canon_ref(str(a)) and _canon_ref(str(a)) in canon_needle for a in aliases):
                    candidates.append(fl)
            if len(candidates) == 1:
                match_basis = "payer alias (third-party payer, no reference)"
            elif len(candidates) > 1:
                who = ", ".join(sorted(c.get("payment_reference", "?") for c in candidates))
                return {"match_status": "unmatched",
                        "reason": f"Multiple payer aliases matched ({who}); manual review"}
        if not candidates:
            return {"match_status": "unmatched", "reason": "No payment reference hit"}
        # If several refs match, keep it in manual review.
        if len(candidates) > 1:
            who = ", ".join(sorted(c.get("payment_reference", "?") for c in candidates))
            return {"match_status": "unmatched",
                    "reason": f"Multiple payment references matched ({who}); manual review"}
        fl = candidates[0]
        open_demands = [dict(r) for r in con.execute(
            "SELECT * FROM rtm_mgmt_demands WHERE flat_id=? AND status NOT IN ('Paid','Superseded','Cancelled') ORDER BY due_date",
            (fl["flat_id"],),
        ).fetchall()]
    if not open_demands:
        return {"match_status": "unmatched", "flat_id": fl["flat_id"], "reason": "Reference matched but no open demand"}
    exact_docs = []
    for d in open_demands:
        outstanding = int(d["amount_pence"]) - allocated_to_demand(d["document_number"])
        if outstanding == amount_pence:
            exact_docs.append(d)
    if len(exact_docs) == 1:
        return {"match_status": "exact", "flat_id": fl["flat_id"], "document_number": exact_docs[0]["document_number"],
                "reason": f"{match_basis} + exact outstanding amount"}
    total_outstanding = sum(max(0, int(d["amount_pence"]) - allocated_to_demand(d["document_number"])) for d in open_demands)
    return {"match_status": "partial", "flat_id": fl["flat_id"], "reason": f"reference hit; amount differs from open demand(s), total open {_money(total_outstanding)}"}


def allocation_review_queue() -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM rtm_mgmt_bank_transactions WHERE match_status IN ('partial','unmatched','duplicate') ORDER BY bank_date DESC, created_at DESC"
        ).fetchall()]
    for r in rows:
        r["amount"] = _money(r["amount_pence"])
    return {"count": len(rows), "transactions": rows}


def allocate_payment(bank_transaction_id: str, allocations: list[dict[str, Any]], auto: bool = False) -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        tx = con.execute("SELECT * FROM rtm_mgmt_bank_transactions WHERE id=?", (bank_transaction_id,)).fetchone()
        if not tx:
            raise ValueError("unknown bank transaction")
        txd = dict(tx)
        already = con.execute("SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_allocations WHERE bank_transaction_id=?", (bank_transaction_id,)).fetchone()["s"] or 0
    allocation_rows = []
    total = 0
    for a in allocations:
        doc = str(a.get("document_number") or a.get("document") or "")
        if not doc:
            raise ValueError("allocation needs document_number")
        amount_pence = int(a.get("amount_pence") if a.get("amount_pence") is not None else _money_to_pence(a.get("amount", "0")))
        if amount_pence <= 0:
            raise ValueError("allocation amount must be positive")
        with connect() as con:
            d = con.execute("SELECT * FROM rtm_mgmt_demands WHERE document_number=?", (doc,)).fetchone()
            if not d:
                raise ValueError(f"unknown demand {doc}")
            dd = dict(d)
        total += amount_pence
        allocation_rows.append((doc, dd["flat_id"], amount_pence))
    if total + int(already) > int(txd["amount_pence"]):
        raise ValueError("allocations exceed bank credit")
    now = utc_now_iso()
    ids = []
    with connect() as con:
        for doc, flat_id, amount_pence in allocation_rows:
            aid = _id("alloc")
            ids.append(aid)
            con.execute(
                "INSERT INTO rtm_mgmt_allocations(id,bank_transaction_id,document_number,flat_id,amount_pence,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                (aid, bank_transaction_id, doc, flat_id, amount_pence, "auto" if auto else "manual", now),
            )
            con.execute(
                """
                INSERT INTO rtm_mgmt_ledger_entries(id,flat_id,fund,schedule,entry_date,kind,description,
                    debit_pence,credit_pence,document_number,bank_transaction_id,payable_id,correction_of,immutable_note,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (_id("led"), flat_id, "service_charge_trust", "sch1", txd["bank_date"], "payment",
                 f"Payment allocated to {doc}", 0, amount_pence, doc, bank_transaction_id, "", "",
                 "Bank credit allocated. Nothing is marked paid without this bank record.", utc_now_iso()),
            )
        con.execute("UPDATE rtm_mgmt_bank_transactions SET allocated_pence=allocated_pence+?, match_status=?, updated_at=? WHERE id=?",
                    (total, "exact" if auto else "reviewed", now, bank_transaction_id))
    for doc, _, _ in allocation_rows:
        refresh_demand_status(doc)
    return {"ok": True, "bank_transaction_id": bank_transaction_id, "allocated": _money(total), "allocation_ids": ids}


def _refresh_wp_mirror_async() -> None:
    """Republish the WordPress read-only mirror (portal snapshot + receipt PDFs + director financials
    summary) OFF-THREAD, best-effort, so a ledger change (a payment recorded) shows up in the portal and
    the director pie chart without waiting for a manual publish. Never raises; no-ops when WordPress is
    not configured, so it is inert in tests and on unconfigured brains."""
    def _run() -> None:
        try:
            from .rtm_portal import (_wp_configured, publish_portal_snapshot,
                                     publish_receipt_pdfs, publish_director_summary)
            if not _wp_configured():
                return
            snap = publish_portal_snapshot()
            if snap.get("ok"):
                try:
                    publish_receipt_pdfs()
                except Exception:
                    pass
                try:
                    publish_director_summary()
                except Exception:
                    pass
        except Exception:
            pass
    try:
        import threading
        threading.Thread(target=_run, daemon=True, name="wp-mirror-refresh").start()
    except Exception:
        pass


def reverse_payment(*, flat: str = "", document_number: str = "") -> dict[str, Any]:
    """Cleanly REVERSE a manually-recorded payment ("unmark paid", true reset per the owner's choice): delete the
    manual bank credit, its allocation + payment ledger entries, the generated receipt (record + portal PDF
    row) and any reserve-trust sweep for this demand, then recompute the demand status (back to
    Issued/Overdue). Targets ONLY source='manual_entry' credits, so a bank-reconciled payment can never be
    silently undone. The demand's own 'demand' ledger entry is untouched (it has no bank_transaction_id).
    Idempotent: a demand with no manual payment returns already_unmarked."""
    _ensure_tables()
    doc = (document_number or "").strip()
    with connect() as con:
        if not doc:
            fid0 = flat.strip().upper().replace("-", "").replace(" ", "")
            row = con.execute(
                "SELECT document_number FROM rtm_mgmt_demands WHERE flat_id=? AND status IN ('Paid','Part-paid') "
                "ORDER BY updated_at DESC LIMIT 1", (fid0,)).fetchone()
            if not row:
                return {"ok": True, "already_unmarked": True, "flat_id": fid0, "note": "no paid demand to reverse"}
            doc = row["document_number"]
        drow = con.execute("SELECT flat_id FROM rtm_mgmt_demands WHERE document_number=?", (doc,)).fetchone()
        if not drow:
            return {"ok": False, "error": f"unknown demand {doc!r}"}
        fid = drow["flat_id"]
        txids = [r["id"] for r in con.execute(
            "SELECT id FROM rtm_mgmt_bank_transactions WHERE matched_document_number=? AND direction='credit' "
            "AND source='manual_entry'", (doc,)).fetchall()]
        if not txids:
            return {"ok": True, "already_unmarked": True, "flat_id": fid, "document_number": doc,
                    "note": "no manually-recorded payment for this demand (nothing to reverse)"}
        removed = {"bank_tx": 0, "allocations": 0, "ledger": 0, "receipts": 0, "portal_pdfs": 0, "reserve": 0}
        for txid in txids:
            removed["allocations"] += con.execute("DELETE FROM rtm_mgmt_allocations WHERE bank_transaction_id=?", (txid,)).rowcount
            removed["ledger"] += con.execute("DELETE FROM rtm_mgmt_ledger_entries WHERE bank_transaction_id=?", (txid,)).rowcount
            removed["bank_tx"] += con.execute("DELETE FROM rtm_mgmt_bank_transactions WHERE id=?", (txid,)).rowcount
        rcpts = [r["receipt_number"] for r in
                 con.execute("SELECT receipt_number FROM rtm_trust_receipts WHERE document_number=?", (doc,)).fetchall()]
        removed["receipts"] += con.execute("DELETE FROM rtm_trust_receipts WHERE document_number=?", (doc,)).rowcount
        for rn in rcpts:
            removed["portal_pdfs"] += con.execute("DELETE FROM portal_receipt_pdfs WHERE receipt_no=?", (rn,)).rowcount
        try:
            removed["reserve"] += con.execute("DELETE FROM rtm_trust_reserve_ledger WHERE document_number=?", (doc,)).rowcount
        except Exception:
            pass
    status = refresh_demand_status(doc)
    return {"ok": True, "flat_id": fid, "document_number": doc, "status": status, "removed": removed,
            "note": "payment reversed; demand reset to unpaid"}


def record_payment(*, flat: str = "", document_number: str = "", amount: Any = None,
                   received_date: str | None = None, reference: str = "", method: str = "Bank transfer",
                   note: str = "") -> dict[str, Any]:
    """Record a payment RECEIVED (money already in the bank): a bank credit + allocation to the flat's
    demand, so the balance clears and the demand shows Paid. This is the one canonical "mark as paid" the
    plugin, Cockpit and chat all call. It RECORDS a receipt; it never initiates or moves money.

    Resolve the demand by document_number, else by flat (its oldest still-open demand). amount defaults to
    the demand's outstanding (a full payment). Idempotent: a demand already cleared returns already_paid.
    """
    _ensure_tables()
    doc = (document_number or "").strip()
    if not doc and not flat:
        return {"ok": False, "error": "flat or document_number is required"}
    with connect() as con:
        if doc:
            row = con.execute("SELECT flat_id, amount_pence FROM rtm_mgmt_demands WHERE document_number=?", (doc,)).fetchone()
            if not row:
                return {"ok": False, "error": f"unknown demand {doc!r}"}
            flat_id, demand_amt = row["flat_id"], int(row["amount_pence"])
        else:
            fid = flat.strip().upper().replace("-", "").replace(" ", "")
            row = con.execute(
                "SELECT document_number, flat_id, amount_pence FROM rtm_mgmt_demands "
                "WHERE flat_id=? AND status NOT IN ('Paid','Superseded','Cancelled') ORDER BY due_date, revision LIMIT 1",
                (fid,)).fetchone()
            if not row:
                # No OPEN demand. The money is still real: record it ON ACCOUNT (an advance for the next
                # budget, or a payment after everything is settled) rather than refusing it. There are no
                # refunds, so it becomes standing credit that the next demand raised will consume. An
                # amount is required here because there is no outstanding to infer one from.
                if amount in (None, ""):
                    st = flat_statement(fid)
                    return {"ok": True, "already_paid": True, "flat_id": fid,
                            "balance": st.get("balance"), "position": st.get("position")}
                return record_on_account_credit(flat=fid, amount=amount, received_date=received_date,
                                                reference=reference, method=method, note=note)
            doc, flat_id, demand_amt = row["document_number"], row["flat_id"], int(row["amount_pence"])
    outstanding = max(0, demand_amt - allocated_to_demand(doc))
    requested = _money_to_pence(amount) if amount not in (None, "") else outstanding
    if requested <= 0:
        # No amount and nothing outstanding: an idempotent no-op on an already-settled demand.
        st = flat_statement(flat_id)
        return {"ok": True, "already_paid": True, "flat_id": flat_id, "document_number": doc,
                "balance": st.get("balance"), "position": st.get("position")}
    fl = _get_flat(flat_id) or {}
    ref = (reference or str(fl.get("payment_reference") or "")).strip()
    name = str(fl.get("leaseholder_name") or flat_id)
    dt = _parse_date(received_date)
    # Allocate what THIS demand needs; anything beyond is a payment on account (no refunds), never dropped.
    alloc = min(requested, outstanding)
    excess = requested - alloc
    txid = _id("bank")
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """INSERT INTO rtm_mgmt_bank_transactions(id,bank_date,description,reference,amount_pence,direction,
               match_status,matched_flat_id,matched_document_number,allocated_pence,duplicate_of,raw_json,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (txid, dt, f"Service charge received, {name} ({flat_id})" + (f", {note}" if note else ""), ref, requested,
             "credit", "reviewed", flat_id, doc, 0, "", json.dumps({"method": method}), "manual_entry", now, now))
    if alloc > 0:
        allocate_payment(txid, [{"document_number": doc, "amount_pence": alloc}])
    if excess > 0:
        # The excess is carried forward as on-account credit on the SAME bank credit, then swept onto any
        # OTHER open demand (oldest first). A true surplus (nothing else open) stays as standing credit.
        _ledger_entry(flat_id=flat_id, entry_date=dt, kind="on_account",
                      description="Payment on account (overpayment)", credit_pence=excess,
                      document_number="", bank_transaction_id=txid, schedule="on_account",
                      note="Overpayment carried forward as on-account credit. No refunds.")
        _apply_credit_to_open_demands(flat_id)
    st = flat_statement(flat_id)
    fully_paid = outstanding > 0 and alloc >= outstanding
    # Auto-generate the branded receipt pack the moment this payment clears the demand, so a paid flat
    # always has its receipt in the portal with no separate step. Best-effort and idempotent: it never
    # blocks or fails the payment recording (soffice absent, template issue, etc. just leave no receipt).
    # receipt_ok is None when no receipt is expected (a part-paid demand issues no receipt). When the
    # demand is settled we CAPTURE the pack result: a soffice/template/render failure must be surfaced
    # (receipt_ok False + receipt_error) instead of leaving a flat "Paid" with no downloadable receipt.
    receipt_ok: bool | None = None
    receipt_error: str | None = None
    if fully_paid:
        receipt_ok = False
        try:
            from .rtm_portal import generate_receipt_pack
            rp = generate_receipt_pack(flat_id=flat_id, document_number=doc)
            if isinstance(rp, dict):
                receipt_ok = bool(rp.get("ok"))
                if not receipt_ok:
                    receipt_error = str(rp.get("reason") or rp.get("error") or "receipt generation failed")
            else:
                receipt_ok = bool(rp)
                if not receipt_ok:
                    receipt_error = "receipt generation returned no result"
        except Exception as exc:
            receipt_ok = False
            receipt_error = str(exc)[:200]
    # Refresh the WordPress mirror in the BACKGROUND so the director pie chart and the per-flat portal
    # balances reflect this payment immediately, instead of going stale until the next manual publish
    # (the "why is the chart still 36%" bug). Best-effort, off-thread: a slow or unreachable WordPress
    # never blocks or fails recording the payment, and it no-ops when WordPress is not configured.
    _refresh_wp_mirror_async()
    return {"ok": True, "flat_id": flat_id, "document_number": doc, "amount": _money(requested),
            "allocated": _money(alloc), "on_account": _money(excess) if excess > 0 else None,
            "available_credit": _money(available_on_account_credit(flat_id)),
            "balance": st.get("balance"), "position": st.get("position"),
            "status": ("Paid" if fully_paid else ("Part-paid" if alloc > 0 else "On account")),
            "receipt_ok": receipt_ok, "receipt_error": receipt_error}


def arrears(today: str | None = None) -> dict[str, Any]:
    _ensure_tables()
    today_s = _parse_date(today)
    refresh_all_demand_statuses(today_s, force=False)
    items = []
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT d.*, f.payment_reference, f.correspondence_address FROM rtm_mgmt_demands d LEFT JOIN rtm_mgmt_flats f ON f.flat_id=d.flat_id WHERE d.status IN ('Overdue','Part-paid','In recovery') AND COALESCE(f.excluded,0)=0 ORDER BY d.due_date"
        ).fetchall()]
    for d in rows:
        outstanding = max(0, int(d["amount_pence"]) - allocated_to_demand(d["document_number"]))
        if outstanding <= 0:
            continue
        days = (date.fromisoformat(today_s) - date.fromisoformat(d["due_date"])).days
        action = _next_recovery_action(days)
        items.append({"document_number": d["document_number"], "flat_id": d["flat_id"], "leaseholder_name": d["leaseholder_name"],
                      "amount_outstanding_pence": outstanding, "amount_outstanding": _money(outstanding),
                      "days_overdue": days, "next_action": action, "due_date": d["due_date"], "status": d["status"]})
    return {"as_at": today_s, "count": len(items), "items": items}


def _next_recovery_action(days_overdue: int) -> dict[str, Any]:
    for threshold, kind, label in ARREARS_TIMELINE:
        if days_overdue >= threshold:
            return {"day_threshold": threshold, "action_type": kind, "label": label}
    return {"day_threshold": 0, "action_type": "not_due", "label": "Not overdue"}


def generate_reminder(document_number: str, action_type: str | None = None, today: str | None = None) -> dict[str, Any]:
    _ensure_tables()
    today_s = _parse_date(today)
    with connect() as con:
        drow = con.execute("SELECT * FROM rtm_mgmt_demands WHERE document_number=?", (document_number,)).fetchone()
        if not drow:
            raise ValueError("unknown demand")
        d = dict(drow)
    paid = allocated_to_demand(document_number)
    outstanding = max(0, int(d["amount_pence"]) - paid)
    days = (date.fromisoformat(today_s) - date.fromisoformat(d["due_date"])).days
    act = action_type or _next_recovery_action(days)["action_type"]
    wording = _reminder_text(d, outstanding, act)
    rid = _id("rec")
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_mgmt_recovery_actions(id,document_number,flat_id,action_type,scheduled_date,status,generated_text,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (rid, document_number, d["flat_id"], act, today_s, "Draft", wording, utc_now_iso(), utc_now_iso()),
        )
        if act in {"final_notice", "debt_referral"}:
            con.execute("UPDATE rtm_mgmt_demands SET status='In recovery', updated_at=? WHERE document_number=?", (utc_now_iso(), document_number))
    return {"ok": True, "id": rid, "action_type": act, "document_number": document_number, "text": wording,
            "note": "Draft only. Operator must review and release; nothing is sent automatically."}


def _reminder_text(d: dict[str, Any], outstanding: int, action_type: str) -> str:
    title = {
        "first_reminder": "Polite service charge reminder",
        "second_reminder": "Second service charge reminder",
        "final_notice": "Final notice before recovery review",
        "debt_referral": "Debt referral pack review",
    }.get(action_type, "Service charge reminder")
    return (
        f"{title}\n\n"
        f"Flat: {d['flat_id']}\nDemand: {d['document_number']}\nPeriod: {d['period']}\n"
        f"Due date: {d['due_date']}\nOutstanding: {_money(outstanding)}\n\n"
        "Our records show the above service charge remains outstanding. Please check your payment reference and contact the RTM if you believe this is incorrect. "
        "This draft deliberately does not add court/forfeiture wording; the operator controls final wording and any statutory summary material."
    )


def release_recovery_action(action_id: str, method: str, release_date: str | None = None, notes: str = "") -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM rtm_mgmt_recovery_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise ValueError("unknown recovery action")
        con.execute("UPDATE rtm_mgmt_recovery_actions SET status='Released', release_date=?, release_method=?, notes=?, updated_at=? WHERE id=?",
                    (_parse_date(release_date), method, notes, utc_now_iso(), action_id))
    return {"ok": True, "id": action_id, "status": "Released", "method": method}


def referral_pack(flat: str | None = None, document_number: str | None = None) -> dict[str, Any]:
    _ensure_tables()
    clauses = []
    params: list[Any] = []
    if flat:
        clauses.append("flat_id=?")
        params.append(flat.strip().upper().replace("-", ""))
    if document_number:
        clauses.append("document_number=?")
        params.append(document_number)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        demands = [dict(r) for r in con.execute(f"SELECT * FROM rtm_mgmt_demands {where} ORDER BY due_date", params).fetchall()]
        actions = [dict(r) for r in con.execute("SELECT * FROM rtm_mgmt_recovery_actions ORDER BY scheduled_date, created_at").fetchall()]
    return {"demands": demands, "recovery_actions": actions, "ledger": [flat_statement(d["flat_id"]) for d in demands],
            "evidence": list_evidence(target_type="demand", target_id=document_number) if document_number else [],
            "note": "Evidence bundle scaffold for solicitor/FTT review. Review before disclosure."}


def add_payable(*, supplier: str, description: str, schedule: str, amount: str | float | int,
                invoice_date: str | None = None, due_date: str | None = None,
                budget_line: str = "", vat_rate: float = 0.0, vat_treatment: str = "",
                cadence: str = "", notes: str = "") -> dict[str, Any]:
    _ensure_tables()
    pid = _id("pay")
    inv = _parse_date(invoice_date)
    due = _parse_date(due_date) if due_date else _add_days(inv, 30)
    now = utc_now_iso()
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_mgmt_payables(id,supplier,description,schedule,budget_line,amount_pence,vat_rate,vat_treatment,
                invoice_date,due_date,cadence,status,notes,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (pid, supplier, description, _norm_schedule(schedule), budget_line, _money_to_pence(amount), float(vat_rate),
             vat_treatment, inv, due, cadence, "Scheduled", notes, now, now),
        )
    return {"ok": True, "id": pid, "due_date": due, "amount": _money(_money_to_pence(amount))}


def approve_payable(payable_id: str) -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        con.execute("UPDATE rtm_mgmt_payables SET status='Approved', approved_at=?, updated_at=? WHERE id=? AND status='Scheduled'",
                    (utc_now_iso(), utc_now_iso(), payable_id))
        if con.total_changes == 0:
            row = con.execute("SELECT status FROM rtm_mgmt_payables WHERE id=?", (payable_id,)).fetchone()
            if not row:
                raise ValueError("unknown payable")
    return {"ok": True, "id": payable_id, "status": "Approved"}


def mark_payable_paid(payable_id: str, amount: str | float | int | None = None, paid_date: str | None = None,
                      bank_reference: str = "") -> dict[str, Any]:
    _ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM rtm_mgmt_payables WHERE id=?", (payable_id,)).fetchone()
        if not row:
            raise ValueError("unknown payable")
        p = dict(row)
        pay_pence = _money_to_pence(amount) if amount is not None else int(p["amount_pence"])
        paid = _parse_date(paid_date)
        con.execute("UPDATE rtm_mgmt_payables SET status='Paid', paid_date=?, paid_amount_pence=?, bank_reference=?, updated_at=? WHERE id=?",
                    (paid, pay_pence, bank_reference, utc_now_iso(), payable_id))
    _ledger_entry(flat_id=None, entry_date=_parse_date(paid_date), kind="supplier_payment", description=f"Paid {p['supplier']}: {p['description']}",
                  credit_pence=pay_pence, payable_id=payable_id, schedule=p["schedule"], note="Supplier payment posted for budget/actuals. Verify against bank statement.")
    return {"ok": True, "id": payable_id, "status": "Paid", "amount": _money(pay_pence)}


def list_payables(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    _ensure_tables()
    where = "WHERE status=?" if status else ""
    params = [status] if status else []
    with connect() as con:
        rows = [dict(r) for r in con.execute(f"SELECT * FROM rtm_mgmt_payables {where} ORDER BY due_date, supplier LIMIT ?", (*params, limit)).fetchall()]
    for r in rows:
        r["amount"] = _money(r["amount_pence"])
        r["paid_amount"] = _money(r.get("paid_amount_pence")) if r.get("paid_amount_pence") is not None else ""
    return rows


def cashflow(months: int = 6) -> dict[str, Any]:
    _ensure_tables()
    today = date.fromisoformat(_today())
    horizons = [1, 3, max(1, months)]
    out = []
    with connect() as con:
        for m in horizons:
            end = today + timedelta(days=31 * m)
            income = con.execute("SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_demands WHERE due_date BETWEEN ? AND ? AND status NOT IN ('Paid','Superseded','Cancelled')", (today.isoformat(), end.isoformat())).fetchone()["s"] or 0
            pay = con.execute("SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_payables WHERE due_date BETWEEN ? AND ? AND status IN ('Scheduled','Approved')", (today.isoformat(), end.isoformat())).fetchone()["s"] or 0
            out.append({"months": m, "expected_income": _money(income), "scheduled_outgoings": _money(pay), "net_expected": _money(int(income) - int(pay))})
    return {"as_at": today.isoformat(), "horizons": out}


def set_budget_line(*, year: str, schedule: str, budget_line: str, amount: str | float | int, notes: str = "",
                    warn_threshold_pence: int | None = None) -> dict[str, Any]:
    """Upsert a budget line. warn_threshold_pence sets an INDEPENDENT warning cap (e.g. Sundries £450);
    pass None to leave any existing threshold unchanged (COALESCE keeps it on conflict)."""
    _ensure_tables()
    bid = hashlib.sha1(f"{year}|{_norm_schedule(schedule)}|{budget_line}".encode()).hexdigest()[:16]
    now = utc_now_iso()
    wt = int(warn_threshold_pence) if warn_threshold_pence is not None else None
    with connect() as con:
        con.execute(
            """
            INSERT INTO rtm_mgmt_budget_lines(id,year,schedule,budget_line,amount_pence,notes,warn_threshold_pence,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(year,schedule,budget_line) DO UPDATE SET amount_pence=excluded.amount_pence,
                notes=excluded.notes, warn_threshold_pence=COALESCE(excluded.warn_threshold_pence, warn_threshold_pence),
                updated_at=excluded.updated_at
            """,
            (bid, str(year), _norm_schedule(schedule), budget_line, _money_to_pence(amount), notes, wt, now, now),
        )
    return {"ok": True, "id": bid, "budget_line": budget_line, "amount": _money(_money_to_pence(amount)),
            "warn_threshold_pence": wt}


def budget_actuals(year: str | None = None) -> dict[str, Any]:
    _ensure_tables()
    if year is None:
        year = str(date.today().year)
    rows = []
    with connect() as con:
        budget_rows = [dict(r) for r in con.execute("SELECT * FROM rtm_mgmt_budget_lines WHERE year=? ORDER BY schedule,budget_line", (str(year),)).fetchall()]
        has_fk = "budget_line_id" in {r[1] for r in con.execute("PRAGMA table_info(rtm_mgmt_payables)").fetchall()}
        has_ev = bool(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='rtm_mgmt_evidence'").fetchone())
        for b in budget_rows:
            # Correlate spend to this line by the budget_line_id FK when the payable carries it, and fall
            # back to the legacy (schedule, budget_line) text match for un-migrated rows (spec D1/R5).
            if has_fk:
                paid_q = ("SELECT COALESCE(SUM(paid_amount_pence),0) s FROM rtm_mgmt_payables "
                          "WHERE status='Paid' AND (budget_line_id=? OR (budget_line_id IS NULL AND schedule=? AND budget_line=?))")
                comm_q = ("SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_payables "
                          "WHERE status IN ('Scheduled','Approved') AND (budget_line_id=? OR (budget_line_id IS NULL AND schedule=? AND budget_line=?))")
                spent = con.execute(paid_q, (b["id"], b["schedule"], b["budget_line"])).fetchone()["s"] or 0
                committed = con.execute(comm_q, (b["id"], b["schedule"], b["budget_line"])).fetchone()["s"] or 0
            else:
                spent = con.execute("SELECT COALESCE(SUM(paid_amount_pence),0) s FROM rtm_mgmt_payables WHERE schedule=? AND budget_line=? AND status='Paid'", (b["schedule"], b["budget_line"])).fetchone()["s"] or 0
                committed = con.execute("SELECT COALESCE(SUM(amount_pence),0) s FROM rtm_mgmt_payables WHERE schedule=? AND budget_line=? AND status IN ('Scheduled','Approved')", (b["schedule"], b["budget_line"])).fetchone()["s"] or 0
            evidence_count = 0
            if has_ev:
                evidence_count = con.execute(
                    "SELECT COUNT(*) c FROM rtm_mgmt_evidence WHERE target_type='budget_line' AND target_id=?",
                    (str(b["id"]),)).fetchone()["c"] or 0
            variance = int(b["amount_pence"]) - int(spent) - int(committed)
            wt = b.get("warn_threshold_pence")
            warn = wt is not None and (int(spent) + int(committed)) > int(wt)
            # Committed outturn = what this line will reach on money ALREADY spent + committed (a floor, not
            # a speculative run-rate that would wildly over-project a one-off annual cost such as insurance
            # or a lift inspection). The flag warns before year-end: 'over' once the committed outturn passes
            # budget, 'over_2x' at more than double it, 'watch' as it approaches.
            bp = int(b["amount_pence"])
            projected = int(spent) + int(committed)
            proj_var = bp - projected
            if bp <= 0:
                pstatus = "over" if projected > 0 else "ok"
            elif projected > 2 * bp:
                pstatus = "over_2x"
            elif projected > bp:
                pstatus = "over"
            elif projected > int(0.85 * bp):
                pstatus = "watch"
            else:
                pstatus = "ok"
            rows.append({"year": year, "schedule": b["schedule"], "budget_line": b["budget_line"],
                         "budget_line_id": b["id"],
                         "budgeted_pence": bp, "spent_pence": int(spent),
                         "committed_pence": int(committed), "variance_pence": variance,
                         "projected_pence": projected, "projected": _money(projected),
                         "projected_variance_pence": proj_var, "projected_variance": _money(proj_var),
                         "projection_status": pstatus,
                         "evidence_count": int(evidence_count),
                         "warn_threshold_pence": (int(wt) if wt is not None else None),
                         "warn_threshold": (_money(int(wt)) if wt is not None else None), "warn": bool(warn),
                         "budgeted": _money(bp), "spent": _money(spent),
                         "committed": _money(committed), "variance": _money(variance),
                         "status": "over_threshold" if warn else ("over_or_committed" if variance < 0 else "within_budget")})
    total = {"budgeted_pence": sum(r["budgeted_pence"] for r in rows),
             "spent_pence": sum(r["spent_pence"] for r in rows),
             "committed_pence": sum(r["committed_pence"] for r in rows),
             "variance_pence": sum(r["variance_pence"] for r in rows),
             "projected_pence": sum(r["projected_pence"] for r in rows),
             "projected_variance_pence": sum(r["projected_variance_pence"] for r in rows)}
    total.update({k.replace("_pence", ""): _money(v) for k, v in total.items()})
    return {"year": year, "lines": rows, "total": total,
            "note": "Feeds year-end cl.12.12 reconciliation. Review with actual lease/budget schedules."}


# Keyword seed mapping a bank payee/description to an EXISTING budget line. Deliberately maps to real
# category names (not the coarse _classify_debit reasons); anything unmatched suggests Sundries. Learned
# rules (rtm_mgmt_expense_rules) take precedence over this seed.
_EXPENSE_KEYWORDS = (
    (("clean",), "sch1", "Cleaning"),
    (("insur",), "sch1", "Buildings insurance"),
    (("docmail", "postage", "stationery"), "sch1", "Stationery and postage"),
    (("management", "admin"), "sch1", "Management and administration"),
    (("electric", "scottishpower", "scottish power", "edf", "octopus"), "sch1", "Communal electricity"),
    (("lift",), "sch1", "Lift maintenance"),
    (("accountant", "accounts fee"), "sch1", "Accounts fee"),
    (("fire risk", "fra"), "sch1", "Fire risk assessment"),
    (("gate",), "sch2", "Gate maintenance"),
    (("pump",), "sch2", "Pump maintenance"),
)
_EXPENSE_STOPWORDS = {"card", "purchase", "payment", "faster", "outward", "inward", "transfer", "direct",
                      "debit", "standing", "order", "limited", "ltd", "the", "and", "invoice", "from", "for",
                      "rtm", "company", "park"}


def _expense_stopwords() -> set[str]:
    """The generic bank-line noise plus the words of the RTM company's own name (site profile), which
    appear on its internal transfers and are never a payee."""
    return _EXPENSE_STOPWORDS | _site()["name_words"]


def _expense_pattern(description: str) -> str:
    """The distinctive payee token to learn from a bank description (the longest non-noise alpha word)."""
    import re as _re
    stop = _expense_stopwords()
    toks = [t.lower() for t in _re.findall(r"[A-Za-z]{4,}", description or "") if t.lower() not in stop]
    return max(toks, key=len) if toks else ""


def suggest_budget_line(description: str) -> dict[str, Any]:
    """Suggest a (schedule, budget_line) for a bank expenditure: learned payee rules first, then the keyword
    seed, else Sundries. Never writes."""
    _ensure_tables()
    d = (description or "").lower()
    with connect() as con:
        rules = [dict(r) for r in con.execute(
            "SELECT pattern, schedule, budget_line FROM rtm_mgmt_expense_rules ORDER BY length(pattern) DESC").fetchall()]
    for r in rules:
        if r["pattern"] and r["pattern"] in d:
            return {"schedule": r["schedule"], "budget_line": r["budget_line"], "source": "learned"}
    for needles, sched, line in _EXPENSE_KEYWORDS:
        if any(n in d for n in needles):
            return {"schedule": sched, "budget_line": line, "source": "keyword"}
    return {"schedule": "sch1", "budget_line": "Sundries", "source": "default"}


def set_expense_rule(*, pattern: str, schedule: str, budget_line: str) -> dict[str, Any]:
    """Remember that a payee pattern (lowercased substring) maps to a budget category, so it auto-suggests
    next time. Upsert on the pattern."""
    _ensure_tables()
    pat = (pattern or "").strip().lower()
    if not pat:
        return {"ok": False, "error": "empty pattern"}
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_mgmt_expense_rules(id,pattern,schedule,budget_line,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(pattern) DO UPDATE SET schedule=excluded.schedule, budget_line=excluded.budget_line",
            (_id("rule"), pat, _norm_schedule(schedule), budget_line, utc_now_iso()))
    return {"ok": True, "pattern": pat, "schedule": _norm_schedule(schedule), "budget_line": budget_line}


def map_expenditure(bank_txn_id: str, schedule: str, budget_line: str, *, learn: bool = True) -> dict[str, Any]:
    """Map a bank EXPENDITURE line to a budget category: create a Paid payable under (schedule, budget_line)
    so it flows into budget_actuals, link the bank row to it, and (learn) remember the payee. Idempotent:
    a line already mapped is a no-op."""
    _ensure_tables()
    with connect() as con:
        row = con.execute("SELECT * FROM rtm_mgmt_bank_transactions WHERE id=?", (bank_txn_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"unknown bank transaction {bank_txn_id!r}"}
        b = dict(row)
    if str(b.get("direction")) != "debit" and str(b.get("match_status")) not in ("expenditure", "expenditure_mapped"):
        return {"ok": False, "error": "not an expenditure line"}
    # Fast path: a plain sequential re-run of an already-mapped line is a no-op.
    if str(b.get("match_status")) == "expenditure_mapped":
        return {"ok": True, "already_mapped": True, "payable_id": b.get("matched_document_number") or ""}
    # ATOMIC CLAIM. The check above is not enough on its own: two calls for the SAME line in one sync batch
    # (or a double-tap in the plugin) can both read 'expenditure', both pass the check, and both create a
    # payable, double-counting the spend against budget. This actually happened (2026-09 D&O: one £128.93
    # payment shown as £257.86). Flip the status in a SINGLE atomic UPDATE guarded on it not already being
    # mapped; only ONE caller wins (rowcount==1) and goes on to create the payable, the rest are no-ops.
    with connect() as con:
        claimed = con.execute(
            "UPDATE rtm_mgmt_bank_transactions SET match_status='expenditure_mapped', updated_at=? "
            "WHERE id=? AND match_status!='expenditure_mapped'", (utc_now_iso(), bank_txn_id)).rowcount
        con.commit()
    if not claimed:
        with connect() as con:
            r2 = con.execute("SELECT matched_document_number FROM rtm_mgmt_bank_transactions WHERE id=?",
                             (bank_txn_id,)).fetchone()
        return {"ok": True, "already_mapped": True,
                "payable_id": (r2["matched_document_number"] if r2 else "") or ""}
    desc = str(b.get("description") or "")
    # We hold the claim. Build the Paid payable and link it back. If that fails, RELEASE the claim (back to
    # 'expenditure') so the line stays retryable rather than stuck 'mapped' with no spend behind it.
    try:
        pay = add_payable(supplier=desc[:120], description=desc, schedule=schedule, amount=_money(int(b["amount_pence"])),
                          invoice_date=str(b.get("bank_date") or ""), budget_line=budget_line,
                          notes=f"From Metro bank line {b.get('bank_date')}")
        pid = pay["id"]
        mark_payable_paid(pid, amount=_money(int(b["amount_pence"])), paid_date=str(b.get("bank_date") or ""),
                          bank_reference=desc[:190])
    except Exception as exc:
        with connect() as con:
            con.execute("UPDATE rtm_mgmt_bank_transactions SET match_status='expenditure', updated_at=? "
                        "WHERE id=? AND COALESCE(matched_document_number,'')=''", (utc_now_iso(), bank_txn_id))
            con.commit()
        return {"ok": False, "error": f"could not map expenditure: {exc}"}
    with connect() as con:
        con.execute("UPDATE rtm_mgmt_bank_transactions SET matched_document_number=?, updated_at=? WHERE id=?",
                    (pid, utc_now_iso(), bank_txn_id))
        con.commit()
    if learn:
        pat = _expense_pattern(desc)
        if pat:
            set_expense_rule(pattern=pat, schedule=schedule, budget_line=budget_line)
    return {"ok": True, "bank_txn_id": bank_txn_id, "payable_id": pid, "schedule": _norm_schedule(schedule),
            "budget_line": budget_line, "amount": _money(int(b["amount_pence"]))}


def reconciliation_view(year: str | None = None, limit: int = 300) -> dict[str, Any]:
    """The bank-reconciliation picture for the director view: recorded credits (how each is matched),
    expenditure lines (mapped category or a suggestion to assign), per-category spend-vs-budget with any
    warning, and the book-vs-bank balances. Aggregates and derived rows only, safe to publish (no raw
    statement JSON)."""
    _ensure_tables()
    year = str(year) if year else "2027"
    with connect() as con:
        # Credits = leaseholder receipts only. Internal sweeps, fund movements, interest and pre-acquisition
        # transfers are NOT leaseholder payments, so they are excluded (they were cluttering the list).
        # Scoped to the current (operating) account: the reserve/service fund accounts reconcile separately.
        cr = [dict(r) for r in con.execute(
            "SELECT id, bank_date, amount_pence, description, reference, matched_flat_id, match_status, "
            "matched_document_number "
            "FROM rtm_mgmt_bank_transactions WHERE direction='credit' AND source='metro_csv' "
            "AND COALESCE(account,'current')='current' "
            "AND match_status NOT IN ('internal_transfer','reserve_movement','service_movement',"
            "'pre_acquisition','interest') "
            "ORDER BY bank_date DESC, created_at DESC LIMIT ?", (limit,)).fetchall()]
        # Expenditure = ONLY genuine supplier payments (assignable to a budget line). An internal transfer,
        # a reserve/service sweep or a pre-acquisition movement is money out but NOT an expense, so it must
        # never appear as an assignable "Sundries" line. This filter is the fix for that clutter.
        # Manual debits are included so a payment entered ahead of its statement is visible, flagged as
        # awaiting the bank line. Once the statement is imported the row is adopted and turns metro_csv,
        # so the same money is never listed twice.
        ex = [dict(r) for r in con.execute(
            "SELECT id, bank_date, amount_pence, description, match_status, matched_document_number, source "
            "FROM rtm_mgmt_bank_transactions WHERE direction='debit' AND source IN ('metro_csv','manual_entry') "
            "AND COALESCE(account,'current')='current' "
            "AND match_status IN ('expenditure','expenditure_mapped') "
            "ORDER BY bank_date DESC, created_at DESC LIMIT ?", (limit,)).fetchall()]
        pay_line = {r["id"]: (r["schedule"], r["budget_line"]) for r in con.execute(
            "SELECT id, schedule, budget_line FROM rtm_mgmt_payables").fetchall()}
    _status_label = {"exact": "allocated", "confirms_manual": "confirmed", "reviewed": "attributed",
                     "unmatched": "unmatched", "pre_acquisition": "pre-acquisition"}
    # Related-evidence links: resolve each bank line to its filed document(s) so the plugin can show a small
    # link to the invoice/receipt/contract. Lazy import avoids a circular dependency with rtm_docs.
    from .rtm_docs import documents_for_bank_line
    credits = [{"bank_date": c["bank_date"], "amount": _money(int(c["amount_pence"])),
                "amount_pence": int(c["amount_pence"]), "description": c["description"],
                "flat_id": c.get("matched_flat_id") or "",
                "state": _status_label.get(str(c["match_status"]), str(c["match_status"])),
                "evidence": documents_for_bank_line(c.get("matched_document_number"), c.get("matched_flat_id"),
                                                    int(c["amount_pence"]), c.get("description"))} for c in cr]
    expenditure = []
    for e in ex:
        mapped = str(e["match_status"]) == "expenditure_mapped"
        line = pay_line.get(str(e.get("matched_document_number") or ""), (None, None)) if mapped else (None, None)
        sug = suggest_budget_line(str(e["description"] or ""))
        expenditure.append({"bank_txn_id": e["id"], "bank_date": e["bank_date"],
                            "amount": _money(int(e["amount_pence"])), "amount_pence": int(e["amount_pence"]),
                            "description": e["description"], "mapped": mapped,
                            "awaiting_bank_line": str(e.get("source")) == "manual_entry",
                            "schedule": line[0] or sug["schedule"], "budget_line": line[1] or sug["budget_line"],
                            "suggestion": sug,
                            "evidence": documents_for_bank_line(e.get("matched_document_number"), None,
                                                                int(e["amount_pence"]), e.get("description"))})
    ba = budget_actuals(year)
    book = None
    try:
        from . import rtm_bookkeeper as BK
        book = BK.summary()
    except Exception:
        book = None
    try:
        funds = fund_reconciliation().get("funds")
    except Exception:
        funds = None
    return {"ok": True, "year": year, "credits": credits, "expenditure": expenditure,
            "budget": ba["lines"], "budget_total": ba["total"],
            "counts": {"credits": len(credits), "expenditure": len(expenditure),
                       "expenditure_unmapped": sum(1 for e in expenditure if not e["mapped"]),
                       "credits_unmatched": sum(1 for c in credits if c["state"] == "unmatched")},
            "funds": funds,
            "book_vs_bank": (book.get("reconciliation") if isinstance(book, dict) else None)}


def add_evidence(*, target_type: str, target_id: str, document_kind: str, title: str,
                 path: str = "", notes: str = "") -> dict[str, Any]:
    _ensure_tables()
    sha = ""
    p = Path(path) if path else None
    if p and p.exists() and p.is_file():
        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        sha = h.hexdigest()
    eid = _id("ev")
    with connect() as con:
        con.execute(
            "INSERT INTO rtm_mgmt_evidence(id,target_type,target_id,document_kind,title,path,sha256,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, target_type, target_id, document_kind, title, str(path or ""), sha, notes, utc_now_iso()),
        )
    return {"ok": True, "id": eid, "sha256": sha}


def list_evidence(target_type: str | None = None, target_id: str | None = None) -> list[dict[str, Any]]:
    _ensure_tables()
    clauses = []
    params: list[Any] = []
    if target_type:
        clauses.append("target_type=?")
        params.append(target_type)
    if target_id:
        clauses.append("target_id=?")
        params.append(target_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as con:
        return [dict(r) for r in con.execute(f"SELECT * FROM rtm_mgmt_evidence {where} ORDER BY created_at DESC", params).fetchall()]


def format_status() -> str:
    pos = fund_position()
    arr = arrears()
    cf = cashflow()
    lines = [f"{_site()['name']} RTM management", "", "Fund/debt position:"]
    for k in ("demands_raised", "payments_allocated", "arrears_outstanding", "paid_outgoings", "committed_outgoings"):
        lines.append(f"  {k.replace('_', ' '):22} {pos[k]}")
    lines.append("")
    lines.append(f"Arrears: {arr['count']} overdue/part-paid demand(s)")
    for it in arr["items"][:10]:
        lines.append(f"  {it['flat_id']} {it['document_number']}: {it['amount_outstanding']} · {it['days_overdue']}d · {it['next_action']['label']}")
    lines.append("")
    lines.append("Cashflow:")
    for h in cf["horizons"]:
        lines.append(f"  {h['months']}m: income {h['expected_income']} / out {h['scheduled_outgoings']} / net {h['net_expected']}")
    return "\n".join(lines)



# --- v0.49 dashboard slice: bulk CSV import + director dashboard + export pack ---
# (ported onto the FIXED spine: these call create_demand/upsert_flat/fund_position etc., so they
#  inherit the Decimal money fix and the supersede-reversal fix automatically.)


def import_flats_csv(path: str | Path) -> dict[str, Any]:
    """Import/update flat reference rows from a CSV without deleting existing rows.

    Expected columns are deliberately flexible: block, flat_number/flat, leaseholder_name/name,
    correspondence_address/address, apportionment_band/apportionment, payment_reference/reference, notes.
    This lets the director start from Land Registry/leaseholder spreadsheets without hand-editing
    every row into CLI commands.
    """
    _ensure_tables()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [{_normalise_header(k or ""): v for k, v in row.items()} for row in reader]
    imported = 0
    errors: list[dict[str, str]] = []
    for idx, row in enumerate(rows, 2):
        try:
            block = _pick(row, "block", "building", "address block")
            flat_no = _pick(row, "flat_number", "flat", "unit", "unit_number")
            # Accept combined <code>F1 style (e.g. the profile's first block, flat 1) in the flat column.
            m = re.fullmatch(rf"\s*({_site()['code_re']})\s*F?\s*(\d+)\s*", str(flat_no or ""), flags=re.I)
            if m and not block:
                block, flat_no = m.group(1).upper(), m.group(2)
            if not block or not flat_no:
                raise ValueError("block and flat_number/flat required")
            upsert_flat(
                block=block,
                flat_number=flat_no,
                leaseholder_name=_pick(row, "leaseholder_name", "leaseholder", "name", "recipient"),
                correspondence_address=_pick(row, "correspondence_address", "address", "service_address", "postal_address"),
                apportionment_band=_pick(row, "apportionment_band", "apportionment", "percentage_band", "percentage"),
                payment_reference=_pick(row, "payment_reference", "reference", "surname", "payment_ref"),
                notes=_pick(row, "notes", "note"),
            )
            imported += 1
        except Exception as e:
            errors.append({"row": str(idx), "error": str(e)})
    return {"ok": not errors, "path": str(path), "imported": imported, "errors": errors}


def import_demands_csv(path: str | Path) -> dict[str, Any]:
    """Import a demand register CSV, creating immutable demand + ledger entries for new docs.

    Existing document_number rows are skipped rather than overwritten, preserving the audit trail.
    """
    _ensure_tables()
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [{_normalise_header(k or ""): v for k, v in row.items()} for row in reader]
    imported = skipped = 0
    errors: list[dict[str, str]] = []
    for idx, row in enumerate(rows, 2):
        doc = _pick(row, "document_number", "document", "demand_number", "doc_no")
        if not doc:
            errors.append({"row": str(idx), "error": "document_number required"})
            continue
        with connect() as con:
            exists = con.execute("SELECT 1 FROM rtm_mgmt_demands WHERE document_number=?", (doc,)).fetchone()
        if exists:
            skipped += 1
            continue
        try:
            revision_s = _pick(row, "revision", "rev") or "0"
            create_demand(
                document_number=doc,
                flat=_pick(row, "flat", "flat_id", "unit", "property"),
                period=_pick(row, "period", "charge_period", "description") or "Service charge demand",
                amount=_pick(row, "amount", "amount_due", "demand_amount", "total"),
                issue_date=_pick(row, "issue_date", "date_issued", "issued"),
                due_date=_pick(row, "due_date", "payment_due"),
                delivery_method=_pick(row, "delivery_method", "delivery", "served_by"),
                revision=int(revision_s or 0),
                supersedes=_pick(row, "supersedes", "supersedes_document_number"),
                schedule=_pick(row, "schedule", "fund", "charge_schedule") or "sch1",
                notes=_pick(row, "notes", "note"),
            )
            imported += 1
        except Exception as e:
            errors.append({"row": str(idx), "document_number": doc, "error": str(e)})
    return {"ok": not errors, "path": str(path), "imported": imported, "skipped_existing": skipped, "errors": errors}


def _ledger_rows(limit: int = 10000) -> list[dict[str, Any]]:
    _ensure_tables()
    with connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM rtm_mgmt_ledger_entries ORDER BY entry_date, created_at, id LIMIT ?",
            (max(1, min(int(limit), 50000)),),
        ).fetchall()]
    for r in rows:
        r["debit"] = _money(r.get("debit_pence"))
        r["credit"] = _money(r.get("credit_pence"))
    return rows


def management_dashboard(today: str | None = None, year: str | None = None) -> dict[str, Any]:
    """One director-facing state object: money, arrears, payables and evidence gaps."""
    _ensure_tables()
    today_s = _parse_date(today)
    status = fund_position()
    arr = arrears(today=today_s)
    review = allocation_review_queue()
    payables = list_payables(limit=500)
    due_soon = [p for p in payables if p["status"] in {"Scheduled", "Approved"} and p.get("due_date") <= _add_days(today_s, 30)]
    actuals = budget_actuals(year)
    over_budget = [l for l in actuals["lines"] if l["variance_pence"] < 0]
    demands = list_demands(limit=5000)
    missing_dispatch = []
    ev = list_evidence(target_type="demand")
    ev_targets = {e["target_id"] for e in ev if e.get("document_kind") in {"dispatch", "docmail-proof", "proof_of_despatch", "service_proof"}}
    for d in demands:
        if d["status"] not in {"Cancelled", "Superseded"} and d["document_number"] not in ev_targets:
            missing_dispatch.append({"document_number": d["document_number"], "flat_id": d["flat_id"], "status": d["status"]})
    next_actions: list[dict[str, Any]] = []
    # 'action' is a plain-English pointer to WHERE the owner does this, never a terminal command: owner
    # journeys go through the plugin/Cockpit, not a CLI (the owner's hard rule).
    if review["count"]:
        next_actions.append({"urgency": "high", "type": "payment_review", "title": f"{review['count']} bank credit(s) need allocation review", "action": "RTM plugin: Bank reconciliation"})
    if arr["count"]:
        next_actions.append({"urgency": "high", "type": "arrears", "title": f"{arr['count']} demand(s) overdue or part-paid", "action": "RTM plugin: Flats and members"})
    if due_soon:
        next_actions.append({"urgency": "medium", "type": "payables", "title": f"{len(due_soon)} supplier payable(s) due in the next 30 days", "action": "RTM plugin: Bank reconciliation (suppliers)"})
    if over_budget:
        next_actions.append({"urgency": "medium", "type": "budget_variance", "title": f"{len(over_budget)} budget line(s) over or fully committed", "action": "RTM plugin: Bank reconciliation (spend against budget)"})
    if missing_dispatch:
        next_actions.append({"urgency": "low", "type": "evidence", "title": f"{len(missing_dispatch)} demand(s) have no recorded dispatch evidence", "action": "RTM plugin: attach dispatch proof to the demand"})
    if not next_actions:
        next_actions.append({"urgency": "low", "type": "ok", "title": "No immediate RTM management actions detected", "action": "Keep reconciling weekly."})
    return {"as_at": today_s, "status": status, "arrears": arr, "review_queue": review,
            "payables_due_soon": due_soon[:25], "budget_actuals": actuals,
            "evidence_gaps": {"demand_dispatch_missing": missing_dispatch[:50], "total_missing_dispatch": len(missing_dispatch)},
            "next_actions": next_actions}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys or ["empty"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})


def export_management_pack(output_dir: str | Path, year: str | None = None) -> dict[str, Any]:
    """Export inspectable CSV/JSON/TXT pack for director, bookkeeper, s.22 or FTT prep."""
    _ensure_tables()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    datasets = {
        "flats.csv": list_flats(),
        "demands.csv": list_demands(limit=10000),
        "bank_review_queue.csv": allocation_review_queue()["transactions"],
        "arrears.csv": arrears()["items"],
        "payables.csv": list_payables(limit=10000),
        "budget_actuals.csv": budget_actuals(year)["lines"],
        "ledger.csv": _ledger_rows(),
        "evidence.csv": list_evidence(),
    }
    for name, rows in datasets.items():
        _write_csv(out / name, rows)
        files.append(str(out / name))
    dash = management_dashboard(year=year)
    (out / "dashboard.json").write_text(json.dumps(dash, indent=2, sort_keys=True), encoding="utf-8")
    files.append(str(out / "dashboard.json"))
    (out / "status.txt").write_text(format_status(), encoding="utf-8")
    files.append(str(out / "status.txt"))
    (out / "README.md").write_text(
        f"# {_site()['name']} RTM management export\n\n"
        "Local export of demands, payments, arrears, payables, budget actuals, ledger and evidence metadata.\n\n"
        "Review before disclosure. This pack is for director/bookkeeper/s.22/FTT preparation and is not a statutory account.\n",
        encoding="utf-8",
    )
    files.append(str(out / "README.md"))
    return {"ok": True, "dir": str(out), "files": files, "file_count": len(files)}


def resolve_flat(val: str) -> tuple[str | None, list[dict[str, Any]] | None, str]:
    """Resolve a flat id or leaseholder name to a single flat id. (flat_id, candidates, error)."""
    v = (val or "").strip()
    if not v:
        return (None, None, "a flat or leaseholder is required")
    up = v.upper().replace("-", "").replace(" ", "")
    flats = list_flats(include_excluded=True) or []
    for f in flats:
        if str(f.get("flat_id", "")).upper() == up:
            return (up, None, "")
    toks = [t for t in v.lower().split() if t]

    def _hay(f: dict[str, Any]) -> str:
        return (str(f.get("leaseholder_name") or "") + " " + str(f.get("payment_reference") or "")).lower()

    matches = [f for f in flats if toks and all(t in _hay(f) for t in toks)]
    # possessive fallback: "martins" -> "martin"
    if not matches and len(toks) == 1 and toks[0].endswith("s") and len(toks[0]) > 3:
        stem = toks[0][:-1]
        matches = [f for f in flats if stem in _hay(f)]
    if len(matches) == 1:
        return (str(matches[0].get("flat_id")), None, "")
    if len(matches) > 1:
        return (None, matches, f"{len(matches)} flats match {val!r}")
    return (None, None, f"no flat or leaseholder matching {val!r}")
