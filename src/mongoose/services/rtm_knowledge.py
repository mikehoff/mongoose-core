"""RTM knowledge base loader + ontology integration.

The knowledge base (M-authored, canonical location ~/.mongoose/data/rtm_knowledge/) is the SOURCE for
domain meaning (semantics.yaml), period state (budget/), records (registry/), obligations (legal/,
operations/), infra (digital/) and narrative (history/, BRIEFING.md, NON-canonical). Its README fixes
the authority order:
    1 encoded canonical functions (rtm_trust/rtm_finance) for computed numbers
    2 semantics.yaml + the active budget for meaning and constants
    3 registry/ for who/where; legal/ operations/ for obligations
    4 history/ for what happened; BRIEFING.md never (orientation only)
    5 the chat model LAST, never for arithmetic

This module reads that base with provenance, and registers its semantics INTO the ontology layer as a
plugin, so the ontology's reflexive audit reconciles the base against the engine (KB <-> rtm_trust). It
never computes money; it names, loads and checks.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

KB_DIRNAME = "rtm_knowledge"
_CACHE: dict[str, dict[str, Any]] = {}


# ── Locate + load ─────────────────────────────────────────────────────────────────────
def kb_root() -> Optional[Path]:
    """Canonical live location first (alongside the brain, survives app updates), then data-dir / cwd
    fallbacks, then an explicit env override. None if not installed."""
    cands: list[Path] = []
    env = os.environ.get("MONGOOSE_RTM_KB_DIR")
    if env:
        cands.append(Path(env).expanduser())
    cands.append(Path("~/.mongoose/data").expanduser() / KB_DIRNAME)
    dd = os.environ.get("MONGOOSE_DATA_DIR")
    if dd:
        cands.append(Path(dd).expanduser() / KB_DIRNAME)
    cands.append(Path("data") / KB_DIRNAME)
    for c in cands:
        if (c / "semantics.yaml").exists():
            return c
    return None


def is_installed() -> bool:
    return kb_root() is not None


def load(force: bool = False) -> dict[str, Any]:
    """All YAML files keyed by relative path, plus BRIEFING.md flagged non-canonical. Cached per root."""
    root = kb_root()
    if root is None:
        return {}
    key = str(root)
    if not force and key in _CACHE:
        return _CACHE[key]
    import yaml
    out: dict[str, Any] = {}
    for p in sorted(root.rglob("*.yaml")):
        rel = str(p.relative_to(root))
        try:
            out[rel] = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception as exc:
            out[rel] = {"_error": str(exc)[:160]}
    briefing = root / "BRIEFING.md"
    if briefing.exists():
        out["BRIEFING.md"] = {"canonical": False, "kind": "narrative_brief",
                              "text": briefing.read_text(encoding="utf-8")}
    _CACHE[key] = out
    return out


def reset() -> None:
    _CACHE.clear()


# ── Typed accessors ───────────────────────────────────────────────────────────────────
def semantics() -> dict[str, Any]:
    return load().get("semantics.yaml", {}) or {}


def flats() -> list[dict[str, Any]]:
    return (load().get("registry/flats.yaml", {}) or {}).get("flats", []) or []


def active_budget() -> dict[str, Any]:
    """The issued budget file (else the only/newest one)."""
    kb = load()
    budgets = {k: v for k, v in kb.items() if k.startswith("budget/") and isinstance(v, dict)}
    issued = [v for v in budgets.values() if (v.get("meta") or {}).get("status") == "issued"]
    if issued:
        return issued[0]
    return next(iter(sorted(budgets.items(), reverse=True)), (None, {}))[1] if budgets else {}


def section(name: str) -> Any:
    """Fetch by relative path (e.g. 'legal/matters.yaml') or by meta kind / 'kind/layer'."""
    kb = load()
    if name in kb:
        return kb[name]
    for _k, v in kb.items():
        if isinstance(v, dict):
            m = v.get("meta") or {}
            if name in (m.get("kind"), f"{m.get('kind')}/{m.get('layer')}"):
                return v
    return None


def index() -> list[dict[str, Any]]:
    """Catalogue with provenance: file, kind, cadence-ish, canonical flag."""
    out = []
    for k, v in load().items():
        m = (v.get("meta") if isinstance(v, dict) else None) or {}
        out.append({"file": k, "kind": m.get("kind"), "layer": m.get("layer"),
                    "canonical": (v.get("canonical", True) if isinstance(v, dict) else True)})
    return out


def search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Substring search over every leaf value, returning path + snippet (for grounding)."""
    q = (query or "").lower().strip()
    hits: list[dict[str, Any]] = []

    def walk(node: Any, path: list[str]) -> None:
        if len(hits) >= limit:
            return
        if isinstance(node, dict):
            for kk, vv in node.items():
                walk(vv, path + [str(kk)])
        elif isinstance(node, list):
            for i, vv in enumerate(node):
                walk(vv, path + [str(i)])
        else:
            s = str(node)
            if q and q in s.lower():
                hits.append({"path": "/".join(path), "value": s[:220]})

    for f, v in load().items():
        if f == "BRIEFING.md":
            continue  # non-canonical; excluded from grounded search
        walk(v, [f])
    return hits[:limit]


def _kb_reserve_fraction() -> float:
    b = active_budget()
    try:
        return float(b["reserve"]["line_pence"]) / float(b["totals_pence"]["schedule_1"])
    except Exception:
        return 0.0


# ── Ontology plugin: register the base's semantics + reconciliation invariants ──────────
def register_knowledge_base(onto) -> None:
    """Register semantics.yaml as ontology concepts/rules at the SITE layer (the owner's authored source wins
    over the hand-written domain v1, exactly as the layering intends), plus invariants that reconcile the
    base against the engine. No-op if the base is not installed."""
    if not is_installed():
        return
    from .rtm_ontology import Concept, Rule, SITE
    sem = semantics()
    src = f"knowledge_base:{kb_root()}/semantics.yaml"

    comp = sem.get("company", {}) or {}
    if comp.get("type_rule"):
        onto.register_concept(Concept(key="company_type", layer=SITE, source=src,
            definition=comp["type_rule"], aliases=["RTM", "RMC", "right to manage company"]))

    orth = sem.get("orthogonality", {}) or {}
    subs = orth.get("sub_accounts", {}) or {}
    scheds = orth.get("schedules", {}) or {}
    if subs:
        onto.register_concept(Concept(key="sub_account", layer=SITE, source=src,
            definition="THREE accounts: " + "; ".join(f"{k} = {v}" for k, v in subs.items())
                       + ". " + orth.get("principle", ""),
            aliases=list(subs.keys()) + ["subaccount", "sub-account", "trust account"],
            distinct_from=["schedule"]))
    if scheds:
        onto.register_concept(Concept(key="schedule", layer=SITE, source=src,
            definition="; ".join(f"{k} = {v}" for k, v in scheds.items()) + ". " + orth.get("principle", ""),
            distinct_from=["sub_account"]))
    if orth.get("the_only_link"):
        onto.register_concept(Concept(key="reserve_link", layer=SITE, source=src,
            definition=orth["the_only_link"]))

    for k, v in (sem.get("identifiers") or {}).items():
        if isinstance(v, str):
            onto.register_concept(Concept(key=f"id_{k}", layer=SITE, source=src, definition=v))
    for k, v in (sem.get("derivations") or {}).items():
        onto.register_rule(Rule(key=f"derive_{k}", kind="value", layer=SITE, value=v, doc=v, source=src))

    onto.register_rule(Rule(key="kb_reserve_fraction", kind="computed", layer=SITE,
        fn="mongoose.services.rtm_knowledge:_kb_reserve_fraction",
        doc="reserve_line / Schedule-1 total, from the ACTIVE knowledge-base budget.", source=src))
    onto.register_rule(Rule(key="kb_reconciles_engine", kind="invariant", layer=SITE,
        fn=_inv_kb_reconciles_engine,
        doc="The knowledge base and the engine (rtm_trust) must agree: same reserve fraction, and the "
            "engine's reserve slice on each band's Schedule-1 half equals the base's pinned figure.",
        source=src))


def _inv_kb_reconciles_engine() -> dict[str, Any]:
    """Reflexive reconciliation: the KB (the owner's spec) and rtm_trust (the engine) are two sources of the same
    truth; they must never diverge. This is the guard that catches a wrong denominator or a drifted pin."""
    try:
        from .rtm_trust import reserve_portion, reserve_fraction as eng_fraction
    except Exception as exc:
        return {"ok": False, "detail": f"engine import failed: {str(exc)[:120]}"}
    budget = active_budget()
    if not budget:
        return {"skipped": True, "detail": "no active KB budget installed"}
    from decimal import Decimal, ROUND_HALF_UP
    kbf = _kb_reserve_fraction()
    ef = eng_fraction()
    if kbf and abs(ef - kbf) > 1e-9:
        return {"ok": False, "detail": f"reserve fraction: engine {ef:.6f} vs KB {kbf:.6f}"}
    try:
        kb_frac = Decimal(int(budget["reserve"]["line_pence"])) / Decimal(int(budget["totals_pence"]["schedule_1"]))
    except Exception:
        return {"skipped": True, "detail": "KB budget missing reserve/schedule_1 totals"}
    bands = budget.get("bands", {}) or {}
    checked = 0
    for band in ("small", "medium", "large"):
        sch1_half = (bands.get(band) or {}).get("sch1_half_pence")
        if sch1_half is None:
            continue
        # KB's own canonical reserve (its budget, its round-half-up) vs the engine's , must be identical.
        kb_reserve = int((Decimal(int(sch1_half)) * kb_frac).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        got = reserve_portion("_kb_reconcile_", int(sch1_half))
        if got != kb_reserve:
            return {"ok": False, "detail": f"{band}: engine reserve {got} != KB {kb_reserve} (sch1_half {sch1_half})"}
        checked += 1
    return {"ok": True, "detail": f"engine reconciles KB reserve fraction + {checked} band reserve slices"}
