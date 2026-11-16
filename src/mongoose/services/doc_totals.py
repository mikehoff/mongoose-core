"""The total of a filed document, with the evidence for it.

A total shown on a document card is used to reconcile trust money, so a wrong one is worse than none. On
2026-09-23 three live documents showed wrong totals (£1.24 for a £40.65 order, £28.16 for £31.80, £1 for
£11.94), each from a different text layout. No single reading rule survives every layout, so this module
does not trust a reading on its own: it collects every labelled amount, then CONFIRMS a total by arithmetic
(subtotal, shipping, VAT and discounts must add up to it), by the ledger (the payment the document is
evidence for), or by the owner. What it cannot confirm it says so, instead of presenting a guess as fact.

confidence, strongest first:
  confirmed   set by the owner
  verified    the document's own figures add up to it, or it matches the payment it is linked to
  likely      a labelled total, not cross-checked
  unverified  no labelled total; the largest amount on the page
  conflict    the document's figures and the linked payment disagree
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# What each label means. Order matters: the first kind whose pattern matches the label wins.
_KINDS: list[tuple[str, str]] = [
    ("order", r"total\s+for\s+\d+\s+items?"),
    ("subtotal", r"(?:items?\)?\s*)?sub[\s-]*total|total\s+before\s+(?:vat|tax)|total\s+excl\w*|net\s+total"
                 r"|item\s+subtotal|total\s+net|amount\s+before\s+(?:vat|tax)"),
    ("vat", r"(?:estimated\s+|total\s+)?(?:vat|tax(?:es)?)(?:\s+subtotal)?"),
    ("discount", r"(?:order\s+)?discounts?|promotions?(?:\s+applied)?|coupons?|vouchers?|(?:total\s+)?savings?"
                 r"|you\s+saved"),
    ("shipping", r"shipping|postage(?:\s*(?:&|and)\s*packing)?|p\s*&\s*p|delivery(?:\s+charges?)?"
                 r"|shipping\s+charges?|carriage"),
    ("paid", r"amount\s+paid|paid(?:\s+to\s+date)?|payments?\s+received"),
    ("payable", r"grand\s+total|invoice\s+total|total\s+(?:due|payable|to\s+pay)|amount\s+(?:due|payable)"
                r"|balance\s+due|total\s+amount|order\s+total"),
    ("total", r"total"),
]
_KIND_RES = [(k, re.compile(rf"^\W*(?:{p})\W*$", re.I)) for k, p in _KINDS]

_AMT = r"[-−]?\s*(?:GBP\s*)?[£€]\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)|([0-9][0-9,]*\.[0-9]{2})(?:\s?[£€]|\s*GBP)?"
_AMOUNT_ONLY = re.compile(rf"^\s*(?:{_AMT})\s*(?:GBP)?\s*$", re.I)
_MONEY = re.compile(r"(?<![0-9.])(-|−)?[ \t]*(?:GBP[ \t]*)?[£€][ \t]?([0-9][0-9,]*(?:\.[0-9]{2})?)(?![0-9.,])"
                    r"|(?<![0-9.,])([0-9][0-9,]*\.[0-9]{2})(?![0-9])[ \t]?(?:[£€]|GBP)", re.I)
_PLAIN_DEC = re.compile(r"(?<![0-9.,])([0-9][0-9,]*\.[0-9]{2})(?![0-9.])")
# "Label ... amount" on one line. The gap may hold a VAT rate ("Total VAT 20% 19.39") or an item count.
_INLINE = re.compile(
    r"(?P<label>[A-Za-z][A-Za-z &()/\-]{1,40}?(?:\s+for\s+\d+\s+items?)?)\s*[:\-]?\s*"
    r"(?:\(?[0-9]{1,2}(?:\.[0-9])?\s*%\)?\s*)?"
    r"(?P<neg>[-−])?\s*(?:GBP\s*)?[£€]?\s?(?P<num>[0-9][0-9,]*\.[0-9]{2})(?![0-9])")


def _d(s: str | None) -> Decimal | None:
    if s is None:
        return None
    try:
        return Decimal(str(s).replace(",", "").replace("−", "-").strip())
    except (InvalidOperation, ValueError):
        return None


# A labelled number with a unit is a measurement, not money: "Total Installed Capacity (kW): 3.74".
_UNIT_LABEL = re.compile(r"\b(?:kw|kwh|kwp|kg|g|mm|cm|m2|m²|litres?|l|hours?|hrs?|mins?|minutes?|miles?|km"
                         r"|capacity|generation|weight)\b|%", re.I)


def _kind(label: str) -> str | None:
    lab = re.sub(r"\s+", " ", (label or "").replace("•", " ")).strip(" :.-")
    if not lab or (_UNIT_LABEL.search(lab) and not re.search(r"total\s+for\s+\d+\s+items?", lab, re.I)):
        return None
    for k, rx in _KIND_RES:
        if rx.match(lab):
            return k
    # "Invoice total £675.00", "Total payable" and similar with extra words either side.
    low = lab.lower()
    if re.search(r"\bsub[\s-]*total\b|\bbefore (?:vat|tax)\b|\bexcl", low):
        return "subtotal"
    if re.search(r"\b(saving|discount|promotion|coupon|voucher)", low):
        return "discount"
    if re.search(r"\b(vat|tax)\b", low) and "total" not in low.replace("vat", ""):
        return "vat"
    if re.search(r"\bgrand total\b|\b(amount|total|balance) (due|payable)\b|\binvoice total\b", low):
        return "payable"
    if re.search(r"\btotal\b", low) and not re.search(r"\b(items?|qty|quantity|weight|units?|pages?)\b", low):
        return "total"
    return None


def _money_values(text: str, *, marked_only: bool = False) -> list[Decimal]:
    """Money amounts on the page. marked_only: just those carrying £/€/GBP, the only ones safe to treat as
    money without a label (a bare 2887.00 on a solar certificate is kWh)."""
    vals: list[Decimal] = []
    for m in _MONEY.finditer(text or ""):
        v = _d(m.group(2) or m.group(3))
        if v is not None:
            vals.append(-v if m.group(1) else v)
    if marked_only:
        return vals
    for m in _PLAIN_DEC.finditer(text or ""):
        v = _d(m.group(1))
        if v is not None and v not in vals:
            vals.append(v)
    return vals


def labelled_amounts(text: str) -> list[tuple[str, Decimal, str]]:
    """(kind, value, label) for every amount the page labels, from same-line pairs and from columns."""
    out: list[tuple[str, Decimal, str]] = []
    lines = [ln.strip() for ln in (text or "").splitlines()]
    for ln in lines:
        for m in _INLINE.finditer(ln):
            label = m.group("label")
            k = _kind(label)
            if not k:
                # The label may be the last words before the amount: "Pay via BACS Total 116.36".
                words = label.split()
                for n in range(min(4, len(words)), 0, -1):
                    k = _kind(" ".join(words[-n:]))
                    if k:
                        label = " ".join(words[-n:])
                        break
            v = _d(m.group("num"))
            if k and v is not None:
                out.append((k, -abs(v) if (m.group("neg") or k == "discount") else v, label.strip()))
    out.extend(_column_pairs([ln for ln in lines if ln]))
    return out


def _column_pairs(lines: list[str]) -> list[tuple[str, Decimal, str]]:
    """Scans and screenshots often extract as a column of labels then a column of amounts. Pair a run of
    label lines with the next run of amount-only lines of the SAME length (trying the trailing labels that
    end in ':' when a heading sits on top). Unequal runs are not guessed at."""
    def is_amount(s: str) -> bool:
        return bool(_AMOUNT_ONLY.match(s))

    def is_label(s: str) -> bool:
        return (not is_amount(s)) and bool(re.search(r"[A-Za-z]", s)) and not re.search(r"[0-9]", s)

    runs: list[tuple[str, int, int]] = []
    i = 0
    while i < len(lines):
        kind = "a" if is_amount(lines[i]) else "l" if is_label(lines[i]) else ""
        j = i
        while j < len(lines) and kind and (is_amount(lines[j]) if kind == "a" else is_label(lines[j])):
            j += 1
        if kind:
            runs.append((kind, i, j))
            i = j
        else:
            i += 1
    out: list[tuple[str, Decimal, str]] = []
    for kind, a, b in runs:
        if kind != "l":
            continue
        k = b
        while k > a and lines[k - 1].endswith(":"):
            k -= 1
        for s, e in [(a, b)] + ([(k, b)] if a < k < b else []):
            if not any(_kind(lines[x]) in ("payable", "total") for x in range(s, e)):
                continue
            nxt = next(((c, d) for kd, c, d in runs if kd == "a" and c >= e and d - c == e - s), None)
            if nxt:
                for lab, amt in zip(lines[s:e], lines[nxt[0]:nxt[1]]):
                    kk = _kind(lab)
                    m = _AMOUNT_ONLY.match(amt)
                    v = _d(m.group(1) or m.group(2)) if m else None
                    if kk and v is not None:
                        neg = amt.lstrip().startswith(("-", "−")) or kk == "discount"
                        out.append((kk, -abs(v) if neg else v, lab))
                return out
    return out


_INVOICE_NO = re.compile(r"\binvoice\s*(?:#|no\.?|number)\s*:?\s*([A-Z0-9][A-Z0-9\-/]{3,})", re.I)


def _close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= Decimal("0.01")


_QTY_PRICE = re.compile(r"£\s?([0-9][0-9,]*\.[0-9]{2})\s*\(\s*([0-9]{1,3})\s*[x×*]\s*£\s?([0-9][0-9,]*\.[0-9]{2})\s*\)", re.I)


def _unlabelled_proof(text: str, on_page: list[Decimal], top: Decimal) -> str | None:
    """Prove the largest amount is the total without labels: a printed "£15.16 (4 × £3.79)", or the other
    amounts on the page (discounts negative) adding up to it exactly. The sum search is bounded to small
    pages and small combinations, and needs at least two parts, so it cannot wander into coincidences on a
    long document full of numbers."""
    for m in _QTY_PRICE.finditer(text or ""):
        tot, n, unit = _d(m.group(1)), int(m.group(2)), _d(m.group(3))
        if tot is not None and unit is not None and _close(tot, top) and _close(unit * n, tot):
            return f"{n} x £{unit} = £{tot}"
    from itertools import combinations
    parts = [v for v in on_page if v != 0 and not _close(abs(v), top)]
    if not (2 <= len(parts) <= 14):
        return None
    for size in range(2, min(5, len(parts)) + 1):
        for combo in combinations(range(len(parts)), size):
            vals = [parts[i] for i in combo]
            if len({abs(v) for v in vals}) < len(vals):
                continue  # the same figure twice (an item price and its subtotal) is one amount, not two
            if _close(sum(vals, Decimal("0")), top):
                shown = " + ".join(f"£{v}" if v > 0 else f"(-£{abs(v)})" for v in vals)
                return f"amounts on the page add up: {shown} = £{top}"
    return None


def compute_total(text: str) -> dict[str, Any]:
    """Best total the document's own content supports: {total, confidence, basis} (total is a Decimal or
    None). Ledger and owner checks are applied on top by resolve_total."""
    t = text or ""
    labelled = labelled_amounts(t)
    on_page = _money_values(t)

    def first(kind: str) -> list[Decimal]:
        return [v for k, v, _ in labelled if k == kind]

    # Several orders on one page (a marketplace order list): the document total is their sum.
    orders = [v for v in first("order") if v > 0]
    if len(orders) >= 2:
        total = sum(orders, Decimal("0"))
        return {"total": total, "confidence": "verified" if any(_close(total, v) for v in on_page) else "likely",
                "basis": f"sum of {len(orders)} order totals ({' + '.join(f'£{o}' for o in orders)})",
                "orders": len(orders)}

    # Several invoices in one file (Amazon puts one per seller in a single PDF): each has its own number
    # and its own "Invoice total", and the document total is their sum, not the largest one.
    inv_nos = list(dict.fromkeys(_INVOICE_NO.findall(t)))
    inv_totals = [v for k, v, lab in labelled if k == "payable" and re.match(r"invoice\s+total", lab, re.I) and v > 0]
    if len(inv_nos) >= 2 and len(inv_totals) == len(inv_nos):
        total = sum(inv_totals, Decimal("0"))
        return {"total": total, "confidence": "verified",
                "basis": f"sum of {len(inv_nos)} invoices ({' + '.join(f'£{v}' for v in inv_totals)})",
                "invoices": len(inv_nos)}

    payable = [v for v in first("payable") if v > 0]
    plain = [v for v in first("total") if v > 0]
    candidates = payable + plain

    # Arithmetic: subtotal + shipping (+ VAT when the subtotal is net) - discounts must land on a figure
    # the document itself prints. Tried with and without VAT, since some subtotals already include it.
    subs = [v for v in first("subtotal") if v > 0]
    ship = sum((v for v in first("shipping") if v > 0), Decimal("0"))
    vat = sum((v for v in first("vat") if v > 0), Decimal("0"))
    disc = sum((abs(v) for v in first("discount")), Decimal("0"))
    for sub in sorted(set(subs), reverse=True):
        for with_vat in (True, False):
            for with_disc in (True, False):
                exp = sub + ship + (vat if with_vat else 0) - (disc if with_disc else 0)
                pool = candidates or on_page
                if exp > 0 and any(_close(exp, v) for v in pool):
                    parts = [f"£{sub} subtotal"] + ([f"£{ship} shipping"] if ship else []) \
                        + ([f"£{vat} VAT"] if with_vat and vat else []) + ([f"-£{disc} discount"] if with_disc and disc else [])
                    if len(parts) > 1 or candidates:
                        return {"total": exp, "confidence": "verified",
                                "basis": " + ".join(parts).replace("+ -", "- ") + f" = £{exp}"}

    # A payable figure that also equals a plain total, or is printed more than once, agrees with itself.
    if payable:
        best = max(payable)
        seen = sum(1 for v in on_page if _close(v, best))
        return {"total": best, "confidence": "verified" if (seen >= 2 or any(_close(best, p) for p in plain)) else "likely",
                "basis": "labelled amount payable" + (" (printed more than once)" if seen >= 2 else "")}
    if plain:
        best = max(plain)
        seen = sum(1 for v in on_page if _close(v, best))
        return {"total": best, "confidence": "verified" if seen >= 2 else "likely",
                "basis": "labelled total" + (" (printed more than once)" if seen >= 2 else "")}
    if len(orders) == 1:
        return {"total": orders[0], "confidence": "likely", "basis": "order total"}
    marked = _money_values(t, marked_only=True)
    positives = [v for v in marked if v > 0]
    if positives:
        top = max(positives)
        proof = _unlabelled_proof(t, marked, top)
        if proof:
            return {"total": top, "confidence": "verified", "basis": proof}
        return {"total": top, "confidence": "unverified",
                "basis": "no labelled total; largest amount on the page"}
    return {"total": None, "confidence": "unverified", "basis": "no amount found"}


def resolve_total(text: str, *, override: str | None = None, ledger_pence: int | None = None,
                  ledger_shared: bool = False) -> dict[str, Any]:
    """compute_total, then the owner's figure (final) and the ledger check. ledger_pence is the payment
    this document is evidence for; ledger_shared means other documents share that payment (then this
    document's total is only a part of it and cannot be checked alone)."""
    ov = _d(override) if override not in (None, "") else None
    if ov is not None and ov > 0:
        return {"total": ov, "confidence": "confirmed", "basis": "set by the owner"}
    r = compute_total(text)
    if ledger_pence and not ledger_shared:
        paid = Decimal(ledger_pence) / 100
        if r["total"] is None or r["confidence"] == "unverified":
            if any(_close(paid, v) for v in _money_values(text)):
                return {"total": paid, "confidence": "verified", "basis": f"matches the £{paid:.2f} payment it is filed against"}
        elif _close(r["total"], paid):
            r = {**r, "confidence": "verified", "basis": r["basis"] + f"; matches the £{paid:.2f} payment"}
        else:
            r = {**r, "confidence": "conflict",
                 "basis": r["basis"] + f"; but the payment it is filed against is £{paid:.2f}"}
    return r


def format_total(r: dict[str, Any]) -> str:
    return f"£{r['total']:.2f}" if r.get("total") is not None else ""
