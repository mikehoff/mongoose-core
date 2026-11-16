"""Multiformat, multimodal RTM financial export.

One engine assembles the RTM's figures ONCE (double-entry journal, per-account bank transactions, budget
vs actual, fund/trust positions + reconciliation, arrears/collection, demands, payables, receipts) and
renders them into whichever formats the caller asks for, then bundles everything (plus the SOURCE
documents as evidence) into a single ZIP an accountant can open.

Formats: accounting CSVs (Xero / QuickBooks Online / GnuCash-generic bank statements), QIF, OFX, an XLSX
workbook, a branded PDF report, and raw JSON. Delivery (Cockpit / chat / plugin) is layered on top by the
callers; this module only builds the pack and returns its bytes + a manifest.

Numbers here mirror the same source functions the Cockpit/plugin already use (collection_position,
fund_position, budget_actuals, reconciliation_view), so an export can never disagree with the live view.
NOT a signed statutory account: it is a bookkeeper-ready pack for review with the accountant/director.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import connect, get_settings

ALL_FORMATS = ("xero", "quickbooks", "gnucash", "qif", "ofx", "xlsx", "pdf", "json")
_ACCOUNTS = ("current", "reserve", "service")


# ---------------------------------------------------------------------------------------------------
# helpers
def _dec(pence: int | None) -> str:
    """Bare decimal string an accounting import expects (no £, no thousands sep): 12893 -> '128.93'."""
    return f"{int(pence or 0) / 100:.2f}"


def _iso(value: Any) -> str:
    """First 10 chars of a date/datetime string -> YYYY-MM-DD (the ISO date every importer accepts)."""
    return str(value or "")[:10]


def _clean_desc(text) -> str:
    """Trim a stored bank description for display: drop a trailing stringified 'null'/'None' reference
    (an import artifact from an earlier reconcile) and collapse whitespace. Never alters figures."""
    s = " ".join(str(text or "").split())
    for junk in (" null", " none"):
        while s.lower().endswith(junk):
            s = s[: -len(junk)].rstrip()
    return s


def _in_range(day: str, since: str | None, until: str | None) -> bool:
    d = _iso(day)
    if since and d < _iso(since):
        return False
    if until and d > _iso(until):
        return False
    return True


def _export_dir() -> Path:
    d = Path(get_settings().db_path).parent / "rtm" / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------------------------------
# data assembly
def _journal(since: str | None, until: str | None) -> list[dict[str, Any]]:
    """The canonical double-entry ledger (rtm_mgmt_ledger_entries), date-filtered (no reader offered one)."""
    from . import rtm_management as M
    rows = M._ledger_rows(limit=50000)
    out = []
    for r in rows:
        if not _in_range(r.get("entry_date"), since, until):
            continue
        out.append({
            "date": _iso(r.get("entry_date")), "fund": r.get("fund") or "", "schedule": r.get("schedule") or "",
            "kind": r.get("kind") or "", "description": r.get("description") or "",
            "flat_id": r.get("flat_id") or "", "document_number": r.get("document_number") or "",
            "debit": _dec(r.get("debit_pence")), "credit": _dec(r.get("credit_pence")),
            "debit_pence": int(r.get("debit_pence") or 0), "credit_pence": int(r.get("credit_pence") or 0),
        })
    return out


def _bank_transactions(since: str | None, until: str | None) -> dict[str, list[dict[str, Any]]]:
    """Real Metro bank movements grouped by account. Amount is SIGNED (credit +, debit -) as bank-statement
    imports (Xero/QuickBooks/QIF/OFX) expect."""
    by_account: dict[str, list[dict[str, Any]]] = {a: [] for a in _ACCOUNTS}
    with connect() as con:
        rows = con.execute(
            "SELECT bank_date, description, reference, amount_pence, direction, "
            "COALESCE(account,'current') account, matched_flat_id, matched_document_number, match_status "
            "FROM rtm_mgmt_bank_transactions WHERE source='metro_csv' AND COALESCE(duplicate_of,'')='' "
            "ORDER BY bank_date, id").fetchall()
    for r in rows:
        d = dict(r)
        if not _in_range(d.get("bank_date"), since, until):
            continue
        signed = int(d["amount_pence"]) * (1 if str(d["direction"]) == "credit" else -1)
        acct = d["account"] if d["account"] in by_account else "current"
        by_account[acct].append({
            "date": _iso(d["bank_date"]), "description": _clean_desc(d.get("description")),
            "payee": (d.get("matched_flat_id") or _clean_desc(d.get("description")))[:120],
            "reference": _clean_desc(d.get("reference")), "amount_pence": signed, "amount": _dec(signed),
            "status": d.get("match_status") or "", "flat_id": d.get("matched_flat_id") or "",
        })
    return by_account


def _assemble(year: str, period: str | None, since: str | None, until: str | None) -> dict[str, Any]:
    """Pull every slice once. Reuses reconciliation_view (credits+expenditure+evidence+budget+funds+book)."""
    from . import rtm_management as M
    from . import rtm_bookkeeper as BK
    from . import rtm_trust as TRUST

    def _safe(fn, default):
        try:
            return fn()
        except Exception as exc:  # a slice must never sink the whole pack
            return {"error": f"{type(exc).__name__}: {exc}", **({} if isinstance(default, dict) else {})} \
                if isinstance(default, dict) else default

    return {
        "meta": {"company": _company_name(), "year": year, "period": period,
                 "since": _iso(since) if since else "", "until": _iso(until) if until else "",
                 "generated_at": datetime.now(timezone.utc).isoformat()},
        "journal": _journal(since, until),
        "bank": _bank_transactions(since, until),
        "budget": _safe(lambda: M.budget_actuals(year), {}),
        "funds": _safe(lambda: M.fund_reconciliation(), {}),
        "reconciliation": _safe(lambda: M.reconciliation_view(year), {}),
        "collection": _safe(lambda: M.collection_position(), {}),
        "arrears": _safe(lambda: M.arrears(), {}),
        "demands": _safe(lambda: M.list_demands(limit=5000), []),
        "payables": _safe(lambda: M.list_payables(limit=5000), []),
        "receipts": _safe(lambda: TRUST.list_receipts(), []),
        "cash_book": _safe(lambda: BK.ledger(), {}),
    }


def _default_company_name() -> str:
    """'<RTM name> RTM Company Limited', the RTM name from the site profile."""
    from .. import profile as _profile
    return f"{_profile.rtm_name()} RTM Company Limited"


def _export_prefix() -> str:
    """Pack filename prefix, e.g. 'Example-Court-RTM-accounts-' (RTM name from the site profile)."""
    from .. import profile as _profile
    return "-".join(_profile.rtm_name().split()) + "-RTM-accounts-"


def _company_name() -> str:
    try:
        from .rtm_templates import BRANDING
        return str(BRANDING.get("name") or _default_company_name())
    except Exception:
        return _default_company_name()


# ---------------------------------------------------------------------------------------------------
# format renderers  (each returns {filename: bytes})
def _csv_bytes(header: list[str], rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _render_xero(bundle) -> dict[str, bytes]:
    # Xero precoded bank-statement CSV, one file per account.
    out = {}
    for acct, rows in bundle["bank"].items():
        if not rows:
            continue
        out[f"xero-bank-{acct}.csv"] = _csv_bytes(
            ["*Date", "*Amount", "Payee", "Description", "Reference"],
            [[r["date"], r["amount"], r["payee"], r["description"], r["reference"]] for r in rows])
    return out


def _render_quickbooks(bundle) -> dict[str, bytes]:
    # QuickBooks Online 3-column bank CSV, one file per account.
    out = {}
    for acct, rows in bundle["bank"].items():
        if not rows:
            continue
        out[f"quickbooks-bank-{acct}.csv"] = _csv_bytes(
            ["Date", "Description", "Amount"],
            [[r["date"], (r["description"] or r["payee"]), r["amount"]] for r in rows])
    return out


def _render_gnucash(bundle) -> dict[str, bytes]:
    # Generic transaction CSV (GnuCash maps columns on import) PLUS the full double-entry journal.
    out = {}
    for acct, rows in bundle["bank"].items():
        if not rows:
            continue
        out[f"gnucash-bank-{acct}.csv"] = _csv_bytes(
            ["Date", "Description", "Reference", "Amount", "Account"],
            [[r["date"], r["description"], r["reference"], r["amount"], acct] for r in rows])
    out["management-ledger.csv"] = _csv_bytes(
        ["Date", "Kind", "Fund", "Schedule", "Flat", "Description", "DocumentNumber", "Debit", "Credit"],
        [[j["date"], j["kind"], j["fund"], j["schedule"], j["flat_id"], j["description"],
          j["document_number"], j["debit"], j["credit"]] for j in bundle["journal"]])
    return out


def _render_qif(bundle) -> dict[str, bytes]:
    out = {}
    for acct, rows in bundle["bank"].items():
        if not rows:
            continue
        lines = ["!Type:Bank"]
        for r in rows:
            lines += [f"D{r['date']}", f"T{r['amount']}", f"P{r['payee']}",
                      f"M{r['description']}", f"N{r['reference']}", "^"]
        out[f"{acct}.qif"] = ("\n".join(lines) + "\n").encode("utf-8")
    return out


def _render_ofx(bundle) -> dict[str, bytes]:
    # Minimal OFX 1.0.2 (SGML) bank statement per account, from a template.
    out = {}
    dt = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    for acct, rows in bundle["bank"].items():
        if not rows:
            continue
        txns = []
        for i, r in enumerate(rows):
            ttype = "CREDIT" if r["amount_pence"] >= 0 else "DEBIT"
            fitid = f"{r['date'].replace('-','')}{i:04d}"
            txns.append(
                f"<STMTTRN><TRNTYPE>{ttype}<DTPOSTED>{r['date'].replace('-','')}"
                f"<TRNAMT>{r['amount']}<FITID>{fitid}"
                f"<NAME>{(r['payee'] or r['description'])[:32]}<MEMO>{r['description'][:255]}</STMTTRN>")
        hdr = ("OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
               "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n")
        body = (
            f"<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS><DTSERVER>{dt}"
            "<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><TRNUID>1"
            "<STATUS><CODE>0<SEVERITY>INFO</STATUS><STMTRS><CURDEF>GBP<BANKACCTFROM><BANKID>MetroBank"
            f"<ACCTID>{acct}<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST><DTSTART>{dt}<DTEND>{dt}"
            + "".join(txns)
            + "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>")
        out[f"{acct}.ofx"] = (hdr + body).encode("ascii", "replace")
    return out


def _render_json(bundle) -> dict[str, bytes]:
    # Everything except the raw *_pence dupes stays; strip nothing sensitive (no extracted_text is present).
    return {"data.json": json.dumps(bundle, indent=1, default=str).encode("utf-8")}


def _render_xlsx(bundle) -> dict[str, bytes]:
    import openpyxl
    from openpyxl.styles import Font
    wb = openpyxl.Workbook()

    def sheet(title, header, rows):
        ws = wb.create_sheet(title[:31])
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)

    wb.remove(wb.active)
    sheet("Ledger", ["Date", "Kind", "Fund", "Schedule", "Flat", "Description", "Doc", "Debit", "Credit"],
          [[j["date"], j["kind"], j["fund"], j["schedule"], j["flat_id"], j["description"],
            j["document_number"], j["debit"], j["credit"]] for j in bundle["journal"]])
    bl = (bundle.get("budget") or {}).get("lines") or []
    sheet("Budget vs Actual", ["Schedule", "Budget line", "Budgeted", "Spent", "Committed", "Variance", "Status"],
          [[b.get("schedule"), b.get("budget_line"), b.get("budgeted"), b.get("spent"), b.get("committed"),
            b.get("variance"), b.get("projection_status")] for b in bl])
    fr = (bundle.get("funds") or {}).get("funds") or {}
    sheet("Fund positions", ["Fund", "Transfers in", "Transfers out", "Interest", "Balance", "Reconciled"],
          [[k, v.get("transfers_in"), v.get("transfers_out"), v.get("interest_earned"),
            v.get("bank_stated_balance") or v.get("computed_balance"), v.get("reconciled")]
           for k, v in fr.items()])
    ar = (bundle.get("arrears") or {}).get("items") or []
    sheet("Arrears", ["Flat", "Leaseholder", "Outstanding", "Days overdue", "Due date", "Status"],
          [[a.get("flat_id"), a.get("leaseholder_name"), a.get("amount_outstanding"),
            a.get("days_overdue"), a.get("due_date"), a.get("status")] for a in ar])
    rc = bundle.get("receipts") or []
    sheet("Receipts", ["Receipt", "Flat", "Doc", "Amount", "Received", "Period"],
          [[r.get("receipt_number"), r.get("flat_id"), r.get("document_number"),
            _dec(r.get("amount_pence")), r.get("received_date"), r.get("period")] for r in rc])
    buf = io.BytesIO()
    wb.save(buf)
    return {"workbook.xlsx": buf.getvalue()}


def _render_pdf(bundle) -> dict[str, bytes]:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=20)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=13, spaceBefore=10)
    meta = bundle["meta"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=18 * mm,
                            leftMargin=18 * mm, rightMargin=18 * mm, title=f"{meta['company']} accounts {meta['year']}")
    e = [Paragraph(meta["company"], h1),
         Paragraph(f"Service-charge accounts {meta['year']} &nbsp; (period {meta.get('period') or 'full year'})", ss["Normal"]),
         Paragraph(f"Prepared {meta['generated_at'][:10]} &middot; bookkeeper pack, not a signed statutory account", ss["Italic"]),
         Spacer(1, 6 * mm)]

    def tbl(title, header, rows):
        e.append(Paragraph(title, h2))
        if not rows:
            e.append(Paragraph("No entries.", ss["Normal"]))
            return
        data = [header] + rows
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2d3d")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f4f4")]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#1f2d3d")),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        e.append(t)

    col = bundle.get("collection") or {}
    e.append(Paragraph("Collection", h2))
    e.append(Paragraph(f"Demanded {col.get('demanded','?')} &middot; collected {col.get('collected','?')} "
                       f"&middot; {col.get('percent_collected','?')} collected", ss["Normal"]))
    bl = (bundle.get("budget") or {}).get("lines") or []
    tbl("Budget vs actual", ["Line", "Budget", "Spent", "Committed", "Variance", ""],
        [[b.get("budget_line", "")[:34], b.get("budgeted"), b.get("spent"), b.get("committed"),
          b.get("variance"), b.get("projection_status")] for b in bl])
    fr = (bundle.get("funds") or {}).get("funds") or {}
    tbl("Fund positions", ["Fund", "In", "Out", "Interest", "Balance", "Reconciled"],
        [[k, v.get("transfers_in"), v.get("transfers_out"), v.get("interest_earned"),
          v.get("bank_stated_balance") or v.get("computed_balance"), "yes" if v.get("reconciled") else "check"]
         for k, v in fr.items()])
    ar = (bundle.get("arrears") or {}).get("items") or []
    tbl("Arrears", ["Flat", "Leaseholder", "Outstanding", "Days", "Status"],
        [[a.get("flat_id"), (a.get("leaseholder_name") or "")[:26], a.get("amount_outstanding"),
          a.get("days_overdue"), a.get("status")] for a in ar])
    doc.build(e)
    return {"report.pdf": buf.getvalue()}


_RENDERERS = {"xero": _render_xero, "quickbooks": _render_quickbooks, "gnucash": _render_gnucash,
              "qif": _render_qif, "ofx": _render_ofx, "xlsx": _render_xlsx, "pdf": _render_pdf,
              "json": _render_json}


# ---------------------------------------------------------------------------------------------------
# evidence bundle
def _evidence_files(bundle) -> tuple[dict[str, bytes], list[list[str]]]:
    """Copy the real source documents linked to each reconciliation line into evidence/, return the files
    plus an index (line -> file + sha256) so every figure traces to its invoice/receipt/contract."""
    from . import rtm_docs as DOCS
    files: dict[str, bytes] = {}
    index: list[list[str]] = []
    seen: set[int] = set()
    recon = bundle.get("reconciliation") or {}
    for kind, rows in (("expenditure", recon.get("expenditure") or []), ("credit", recon.get("credits") or [])):
        for row in rows:
            for ev in (row.get("evidence") or []):
                did = ev.get("doc_id")
                if did is None:
                    continue
                index.append([kind, row.get("bank_date", ""), row.get("amount", ""),
                              (row.get("description") or "")[:60], str(ev.get("filename") or "")])
                if did in seen:
                    continue
                seen.add(did)
                try:
                    doc = DOCS.get_document(int(did))
                    p = Path(str((doc or {}).get("stored_path") or "")).expanduser()
                    if not p.exists():
                        continue
                    raw = p.read_bytes()
                except Exception:
                    continue
                safe = f"evidence/{int(did)}_{Path(str(ev.get('filename') or 'document')).name}"[:180]
                files[safe] = raw
    return files, index


# ---------------------------------------------------------------------------------------------------
# public entry point
def build_export(*, year: str = "2027", period: str | None = None, since: str | None = None,
                 until: str | None = None, formats: list[str] | None = None,
                 include_evidence: bool = True) -> dict[str, Any]:
    """Assemble the pack and return {ok, path, filename, bytes, manifest}. `since` defaults to the RTM
    acquisition date so an export covers the RTM's life to date unless a range is given."""
    from . import rtm_management as M
    fmts = [f for f in (formats or list(ALL_FORMATS)) if f in _RENDERERS]
    if not fmts:
        return {"ok": False, "error": f"no valid formats; choose from {', '.join(ALL_FORMATS)}"}
    if since is None:
        since = getattr(M, "RTM_ACQUISITION_DATE", "2026-07-16")

    bundle = _assemble(year, period, since, until)
    members: dict[str, bytes] = {}
    rendered: list[str] = []
    for f in fmts:
        try:
            members.update(_RENDERERS[f](bundle))
            rendered.append(f)
        except Exception as exc:
            members[f"_ERROR_{f}.txt"] = f"{f} render failed: {type(exc).__name__}: {exc}".encode("utf-8")

    evidence_index: list[list[str]] = []
    if include_evidence:
        ev_files, evidence_index = _evidence_files(bundle)
        members.update(ev_files)
        if evidence_index:
            members["evidence/index.csv"] = _csv_bytes(
                ["line_type", "date", "amount", "description", "document"], evidence_index)

    manifest = {"company": bundle["meta"]["company"], "year": year, "period": period,
                "since": bundle["meta"]["since"], "until": bundle["meta"]["until"],
                "generated_at": bundle["meta"]["generated_at"], "formats": rendered,
                "evidence_files": sum(1 for k in members if k.startswith("evidence/") and k != "evidence/index.csv"),
                "files": sorted(members.keys())}
    readme = (
        f"{bundle['meta']['company']} - service-charge accounts export\n"
        f"Year {year}{' period ' + period if period else ''}; covers {bundle['meta']['since']} to "
        f"{bundle['meta']['until'] or 'date'}.\n\n"
        "This is a BOOKKEEPER-READY pack for review with your accountant/director, NOT a signed statutory\n"
        "account. Formats included: " + ", ".join(rendered) + ".\n\n"
        "- xero-bank-*.csv / quickbooks-bank-*.csv / gnucash-bank-*.csv: bank statements per account.\n"
        "- management-ledger.csv: categorised transaction ledger (every demand raised, payment received,\n"
        "  on-account credit and supplier payment; one row per event, NOT a balanced trial balance -\n"
        "  the authoritative cash record is the per-account bank statements plus the reconciliation).\n"
        "- *.qif / *.ofx: universal bank interchange per account.\n"
        "- workbook.xlsx: journal, budget-vs-actual, fund positions, arrears, receipts.\n"
        "- report.pdf: readable summary on RTM letterhead.\n"
        "- evidence/: the source invoices/receipts/contracts, with index.csv tying each figure to its file.\n")
    members["README.txt"] = readme.encode("utf-8")
    members["manifest.json"] = json.dumps(manifest, indent=1).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    zip_bytes = buf.getvalue()

    # Time in the stamp so each build is a DISTINCT file: a live download token points at one immutable
    # ZIP and is never silently replaced by a later same-day rebuild.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    prefix = _export_prefix()
    fname = f"{prefix}{year}-{stamp}.zip"
    ed = _export_dir()
    path = ed / fname
    try:
        path.write_bytes(zip_bytes)
    except Exception:
        path = None  # bytes still returned; a caller can push them without the on-disk copy
    # Bound the folder: keep the newest 12 packs, delete older ones so occasional exports never pile up.
    try:
        old = sorted(ed.glob(f"{prefix}*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
        for stale in old[12:]:
            stale.unlink(missing_ok=True)
    except Exception:
        pass

    return {"ok": True, "path": (str(path) if path else ""), "filename": fname,
            "bytes": zip_bytes, "size": len(zip_bytes), "manifest": manifest}
