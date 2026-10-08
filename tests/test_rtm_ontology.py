"""The RTM ontology layer: layered override, rules, plugins, reflective describe/explain, reflexive audit."""
from mongoose.services.rtm_ontology import Ontology, Concept, Rule, DOMAIN, OVERRIDE


def _base():
    o = Ontology()
    # load only the built-ins we want deterministically (no file overrides, no singleton)
    from mongoose.services import rtm_ontology as M
    M._register_core(o)
    M._register_rtm_domain(o)
    return o


def test_describe_is_reflective():
    o = _base()
    d = o.describe()
    assert [l["name"] for l in d["layers"]] == ["base", "domain", "site", "override"]
    # the disambiguation that was the whole bug is explicit and machine-readable
    assert "schedule" in d["concepts"]["sub_account"]["distinct_from"]
    assert "sub_account" in d["concepts"]["schedule"]["distinct_from"]
    assert "reserve_fraction" in d["rules"] and "subaccount_split" in d["rules"]


def test_layered_override_wins():
    o = _base()
    # a value rule at a higher layer overrides the domain one
    o.register_rule(Rule(key="reserve_rounding", kind="value", layer=OVERRIDE, value="round-half-even"))
    assert o.value("reserve_rounding") == "round-half-even"
    ex = o.explain("reserve_rounding")
    assert ex["rule"]["effective_layer"] == "override"
    assert "domain" in ex["rule"]["shadowed"]


def test_pluggable():
    o = _base()
    def plugin(onto):
        onto.register_concept(Concept(key="section_20", definition="A consultation.", layer=DOMAIN))
        onto.register_rule(Rule(key="answer", kind="value", value=42))
    o.register_plugin(plugin)
    for fn in o._plugins:
        fn(o)
    assert o.concept("section_20") is not None
    assert o.value("answer") == 42


def test_computed_rule_is_live_from_engine():
    o = _base()
    # computed rule resolves the engine function and evaluates it (never a stored copy)
    v = o.value("reserve_fraction")
    assert abs(v - 4710 / 30320) < 1e-9


def test_audit_is_reflexive_and_green():
    o = _base()
    rep = o.audit()
    # no hard failures; invariants either pass or skip (skip when the test DB has no budget/demands)
    assert rep["failed"] == 0
    assert rep["ok"] is True


def test_audit_catches_dangling_disambiguation():
    o = _base()
    o.register_concept(Concept(key="ghost", definition="x", distinct_from=["does_not_exist"]))
    rep = o.audit()
    assert rep["ok"] is False
    assert any("does_not_exist" in c["check"] and c["ok"] is False for c in rep["checks"])
