"""RTM ontology layer, the domain's canonical *meaning*, separate from data (rows) and behaviour
(functions). This is the thing that was missing: a single place that says what a "sub-account" is, that
it is NOT a "schedule", how the reserve slice is computed, and which function is the authority for it.

Design goals (all first-class here):
  - LAYER BASED   : concepts/rules live in ordered layers; a higher layer overrides a lower one for the
                    same key. base < domain < site < override. The override layer can come from a plain
                    JSON file, so anything can be overridden WITHOUT code.
  - RULE BASED    : knowledge is Rules. kind = 'value' (a constant/definition), 'computed' (bound to a
                    function, evaluated live so it can never drift from the engine), or 'invariant' (a
                    self-check the auditor runs).
  - PLUGGABLE     : register_plugin(fn) adds concepts/rules; the RTM domain itself is just the first
                    plugin. New domains/overrides plug in the same way.
  - REFLECTIVE    : describe()/explain() report every concept, rule, layer and provenance, the ontology
                    can be read and cited (by the chat tool, an endpoint, a human).
  - REFLEXIVE     : audit() runs the invariants + coverage checks over ITSELF (spec<->code<->function
                    reconciliation, no orphan concepts, every computed rule's fn resolves). This is the
                    knowledge-auditor: the guard that would have caught the reserve 2p drift.

Nothing here recomputes financial figures: computed rules delegate to the engine (rtm_trust, etc.), so
the model narrates the engine and the ontology only names and audits it.
"""
from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


# ── Layers ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Layer:
    name: str
    priority: int  # higher wins on a key clash


BASE = Layer("base", 0)         # generic, cross-domain terms
DOMAIN = Layer("domain", 10)    # RTM domain knowledge
SITE = Layer("site", 50)        # this site's specifics (the RTM company in the site profile)
OVERRIDE = Layer("override", 100)  # user/file/DB overrides , win over everything


# ── Units of knowledge ──────────────────────────────────────────────────────────────
@dataclass
class Concept:
    """A named domain term and, crucially, what it is NOT (distinct_from) so the two splits that were
    conflated can never be conflated again."""
    key: str
    definition: str
    aliases: list[str] = field(default_factory=list)
    distinct_from: list[str] = field(default_factory=list)
    kind: str = "term"
    source: str = ""
    layer: Layer = DOMAIN


@dataclass
class Rule:
    key: str
    kind: str                       # 'value' | 'computed' | 'invariant'
    layer: Layer = DOMAIN
    value: Any = None               # 'value': a literal (or a zero-arg callable)
    fn: Any = None                  # 'computed'/'invariant': a callable or "module:func" string
    doc: str = ""
    source: str = ""


def _resolve_callable(fn: Any) -> Optional[Callable]:
    """Accept a callable or a 'module.path:function' string (so overrides from a file can point at code)."""
    if callable(fn):
        return fn
    if isinstance(fn, str) and ":" in fn:
        mod, _, name = fn.partition(":")
        try:
            return getattr(importlib.import_module(mod), name)
        except Exception:
            return None
    return None


# ── Registry ────────────────────────────────────────────────────────────────────────
class Ontology:
    def __init__(self) -> None:
        self._layers: dict[str, Layer] = {l.name: l for l in (BASE, DOMAIN, SITE, OVERRIDE)}
        self._concepts: dict[str, list[Concept]] = {}
        self._rules: dict[str, list[Rule]] = {}
        self._plugins: list[Callable[["Ontology"], None]] = []
        self._loaded = False

    # -- registration (pluggable) --
    def add_layer(self, layer: Layer) -> None:
        self._layers[layer.name] = layer

    def register_concept(self, c: Concept) -> None:
        self._concepts.setdefault(c.key, []).append(c)

    def register_rule(self, r: Rule) -> None:
        self._rules.setdefault(r.key, []).append(r)

    def register_plugin(self, fn: Callable[["Ontology"], None]) -> None:
        """A plugin is any fn(ontology) that registers concepts/rules. The RTM domain is the first one."""
        self._plugins.append(fn)

    # -- resolution (layer based: highest priority wins) --
    def concept(self, key: str) -> Optional[Concept]:
        items = self._concepts.get(key)
        return max(items, key=lambda x: x.layer.priority) if items else None

    def rule(self, key: str) -> Optional[Rule]:
        items = self._rules.get(key)
        return max(items, key=lambda x: x.layer.priority) if items else None

    def value(self, key: str) -> Any:
        """Resolve a rule to its live value. 'computed' rules are evaluated now, so they always reflect
        the engine and can never go stale."""
        r = self.rule(key)
        if r is None:
            raise KeyError(f"no rule {key!r}")
        if r.kind == "value":
            return r.value() if callable(r.value) else r.value
        if r.kind == "computed":
            fn = _resolve_callable(r.fn)
            if fn is None:
                raise RuntimeError(f"computed rule {key!r} has no resolvable function")
            return fn()
        raise RuntimeError(f"rule {key!r} is kind {r.kind!r}, not a value")

    # -- reflective: read the ontology --
    def describe(self) -> dict[str, Any]:
        def _c(c: Concept) -> dict[str, Any]:
            return {"key": c.key, "definition": c.definition, "aliases": c.aliases,
                    "distinct_from": c.distinct_from, "layer": c.layer.name, "source": c.source}

        def _r(r: Rule) -> dict[str, Any]:
            return {"key": r.key, "kind": r.kind, "layer": r.layer.name, "doc": r.doc, "source": r.source}

        return {
            "layers": sorted(({"name": l.name, "priority": l.priority} for l in self._layers.values()),
                             key=lambda x: x["priority"]),
            "concepts": {k: _c(self.concept(k)) for k in sorted(self._concepts)},
            "rules": {k: _r(self.rule(k)) for k in sorted(self._rules)},
        }

    def explain(self, key: str) -> dict[str, Any]:
        """Where a concept/rule's effective value comes from, and what it shadows across layers."""
        out: dict[str, Any] = {"key": key}
        if key in self._concepts:
            layers = sorted(self._concepts[key], key=lambda x: -x.layer.priority)
            out["concept"] = {"effective_layer": layers[0].layer.name,
                              "definition": layers[0].definition,
                              "distinct_from": layers[0].distinct_from,
                              "shadowed": [c.layer.name for c in layers[1:]]}
        if key in self._rules:
            layers = sorted(self._rules[key], key=lambda x: -x.layer.priority)
            eff = layers[0]
            entry: dict[str, Any] = {"effective_layer": eff.layer.name, "kind": eff.kind, "doc": eff.doc,
                                     "shadowed": [r.layer.name for r in layers[1:]]}
            if eff.kind in ("value", "computed"):
                try:
                    entry["value"] = self.value(key)
                except Exception as exc:
                    entry["value_error"] = str(exc)[:160]
            out["rule"] = entry
        if "concept" not in out and "rule" not in out:
            out["error"] = "unknown key"
        return out

    # -- reflexive: audit the ontology over itself --
    def audit(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: Optional[bool], detail: str = "") -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})  # ok=None => skipped

        # Coverage 1: every distinct_from target is itself a known concept (no dangling disambiguation).
        for k in self._concepts:
            for other in (self.concept(k).distinct_from or []):
                add(f"concept '{k}' distinct_from '{other}' exists",
                    other in self._concepts, "" if other in self._concepts else "unknown concept")
        # Coverage 2: every computed rule's function resolves and is callable.
        for k, items in self._rules.items():
            r = self.rule(k)
            if r.kind == "computed":
                add(f"computed rule '{k}' fn resolves", _resolve_callable(r.fn) is not None,
                    "" if _resolve_callable(r.fn) else "function not importable/callable")
        # Invariants: run every 'invariant' rule. Its fn returns {ok, detail} or {skipped, detail}.
        for k, items in self._rules.items():
            r = self.rule(k)
            if r.kind != "invariant":
                continue
            fn = _resolve_callable(r.fn)
            if fn is None:
                add(f"invariant '{k}'", False, "function not resolvable")
                continue
            try:
                res = fn() or {}
                if res.get("skipped"):
                    add(f"invariant '{k}'", None, res.get("detail", "skipped (data absent)"))
                else:
                    add(f"invariant '{k}'", bool(res.get("ok")), res.get("detail", ""))
            except Exception as exc:
                add(f"invariant '{k}'", False, f"raised: {str(exc)[:160]}")

        failures = [c for c in checks if c["ok"] is False]
        skipped = [c for c in checks if c["ok"] is None]
        return {"ok": not failures, "failed": len(failures), "skipped": len(skipped),
                "passed": sum(1 for c in checks if c["ok"] is True), "checks": checks}

    # -- load: idempotent bring-up of built-ins + plugins + file overrides --
    def load(self) -> "Ontology":
        if self._loaded:
            return self
        _register_core(self)
        _register_rtm_domain(self)
        # the owner's authored knowledge base registers its semantics at the SITE layer (overrides the hand-written
        # domain v1) and adds the KB<->engine reconciliation invariant. No-op if the base is not installed.
        try:
            from .rtm_knowledge import register_knowledge_base
            register_knowledge_base(self)
        except Exception:
            pass
        for fn in list(self._plugins):
            try:
                fn(self)
            except Exception:
                pass
        _load_file_overrides(self)
        self._loaded = True
        return self


# ── Built-in content ────────────────────────────────────────────────────────────────
def _register_core(o: Ontology) -> None:
    o.register_concept(Concept(
        key="ontology", layer=BASE, kind="meta",
        definition="The canonical meaning layer: concepts and rules, layered and overridable, audited over itself.",
        source="rtm_ontology._register_core"))


def _flat_id_definition() -> str:
    """'Internal flat identifier like 1ECF1 (block 1EC, flat 1).', with the site profile's first block."""
    from .. import profile as _profile
    code = _profile.block_codes()[0]
    return f"Internal flat identifier like {code}F1 (block {code}, flat 1)."


def _register_rtm_domain(o: Ontology) -> None:
    src = "rtm_ontology._register_rtm_domain"
    # -- concepts, with the disambiguation that was the whole problem --
    o.register_concept(Concept(
        key="schedule", definition="A cost schedule the DEMAND is apportioned across: sch1 'Building "
        "service costs', sch2 'Undercroft parking'. How much a flat is CHARGED.",
        aliases=["cost schedule", "sch1", "sch2"], distinct_from=["sub_account"], source=src))
    o.register_concept(Concept(
        key="sub_account", definition="A TRUST account received money is SWEPT into: reserve_fund_trust, "
        "service_charge_trust. Where the MONEY GOES after it is paid. Orthogonal to schedules.",
        aliases=["subaccount", "sub-account", "trust account", "trust fund"],
        distinct_from=["schedule"], source=src))
    o.register_concept(Concept(
        key="reserve_fund_trust", definition="Ring-fenced reserve trust. Fed only by the reserve slice of "
        "the Schedule-1 half; undercroft never contributes.", aliases=["reserve", "reserve_trust"], source=src))
    o.register_concept(Concept(
        key="service_charge_trust", definition="Operational service-charge trust. Receives the whole "
        "demand minus the reserve slice.", aliases=["service_trust", "service"], source=src))
    o.register_concept(Concept(
        key="payment_reference", definition="A flat's payment reference is the leaseholder SURNAME "
        "(uppercase), e.g. MORGAN, NOT the flat id.", distinct_from=["flat_id"], source=src))
    o.register_concept(Concept(
        key="flat_id", definition=_flat_id_definition(),
        distinct_from=["payment_reference"], source=src))

    # -- rules (computed ones delegate to the engine, never recompute here) --
    o.register_rule(Rule(
        key="reserve_fraction", kind="computed", fn="mongoose.services.rtm_trust:reserve_fraction",
        doc="Reserve line / Schedule-1 total (4710/30320 = 15.5343%), pulled live from the budget.", source=src))
    o.register_rule(Rule(
        key="subaccount_split", kind="computed", fn="mongoose.services.rtm_trust:subaccount_split",
        doc="THE authority for a flat's reserve/service trust split. reserve = round-half-up(Sch1 half x "
            "reserve_fraction); service = whole demand - reserve. Undercroft contributes zero reserve.",
        source=src))
    o.register_rule(Rule(
        key="reserve_rounding", kind="value", value="round-half-up on pence",
        doc="The canonical rounding for the reserve slice.", source=src))

    # -- invariants (the reflexive guard) --
    o.register_rule(Rule(key="reserve_fraction_matches_budget", kind="invariant",
                         fn=_inv_reserve_fraction_matches_budget,
                         doc="reserve_fraction() must equal the reserve line / Schedule-1 total in the live budget.",
                         source=src))
    o.register_rule(Rule(key="subaccount_split_reconciles", kind="invariant",
                         fn=_inv_subaccount_split_reconciles,
                         doc="For every flat with an issued demand, the canonical split total must equal the "
                             "demand and reserve+service must not leak a penny.",
                         source=src))


# ── Invariant implementations (degrade to skipped when the DB has no data) ────────────
def _inv_reserve_fraction_matches_budget() -> dict[str, Any]:
    try:
        from .rtm_trust import reserve_fraction, _budget_reserve_and_sch1
        reserve, sch1 = _budget_reserve_and_sch1()
        if not sch1:
            return {"skipped": True, "detail": "no Schedule-1 budget loaded"}
        expected = reserve / sch1
        got = reserve_fraction()
        ok = abs(got - expected) < 1e-9
        return {"ok": ok, "detail": f"reserve_fraction={got:.6f} vs reserve/sch1={expected:.6f} ({reserve}/{sch1})"}
    except Exception as exc:
        return {"ok": False, "detail": f"raised: {str(exc)[:140]}"}


def _inv_subaccount_split_reconciles() -> dict[str, Any]:
    try:
        from .rtm_management import list_flats, list_demands
        from .rtm_trust import subaccount_split
        checked = 0
        for f in (list_flats() or []):
            fid = str(f.get("flat_id") or "")
            dem = [d for d in (list_demands(flat=fid) or []) if d.get("amount_pence", 0) > 0]
            if not dem:
                continue
            s = subaccount_split(fid)
            if s["total"] != dem[0]["amount"] or s["reserve_pence"] + s["service_pence"] != s["total_pence"]:
                return {"ok": False, "detail": f"{fid}: split {s['total']} vs demand {dem[0]['amount']}"}
            checked += 1
        if checked == 0:
            return {"skipped": True, "detail": "no issued demands to reconcile"}
        return {"ok": True, "detail": f"{checked} flats reconcile to the penny"}
    except Exception as exc:
        return {"ok": False, "detail": f"raised: {str(exc)[:140]}"}


# ── File overrides (easy override, no code) ───────────────────────────────────────────
def _overrides_path() -> str:
    base = os.environ.get("MONGOOSE_DATA_DIR") or os.path.expanduser("~/.mongoose/data")
    return os.path.join(base, "rtm_ontology_overrides.json")


def _load_file_overrides(o: Ontology) -> None:
    """An optional JSON file lets a human override any concept/rule value at the top layer, no code:
    {"rules": {"reserve_rounding": {"kind":"value","value":"round-half-even"}},
     "concepts": {"sub_account": {"definition":"..."}}}"""
    path = _overrides_path()
    try:
        if not os.path.exists(path):
            return
        data = json.loads(open(path, encoding="utf-8").read()) or {}
    except Exception:
        return
    for key, spec in (data.get("concepts") or {}).items():
        o.register_concept(Concept(key=key, layer=OVERRIDE, source=f"override:{path}",
                                   definition=str(spec.get("definition", "")),
                                   aliases=list(spec.get("aliases", []) or []),
                                   distinct_from=list(spec.get("distinct_from", []) or [])))
    for key, spec in (data.get("rules") or {}).items():
        o.register_rule(Rule(key=key, layer=OVERRIDE, source=f"override:{path}",
                             kind=str(spec.get("kind", "value")), value=spec.get("value"),
                             fn=spec.get("fn"), doc=str(spec.get("doc", ""))))


# ── Module singleton + public helpers ─────────────────────────────────────────────────
_ONTOLOGY: Optional[Ontology] = None


def ontology() -> Ontology:
    global _ONTOLOGY
    if _ONTOLOGY is None:
        _ONTOLOGY = Ontology().load()
    return _ONTOLOGY


def reset() -> None:
    """Drop the singleton (tests, or after writing an override file)."""
    global _ONTOLOGY
    _ONTOLOGY = None


def describe() -> dict[str, Any]:
    return ontology().describe()


def explain(key: str) -> dict[str, Any]:
    return ontology().explain(key)


def audit() -> dict[str, Any]:
    return ontology().audit()


def value(key: str) -> Any:
    return ontology().value(key)
