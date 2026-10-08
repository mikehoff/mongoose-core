"""The inherited entity-matching mechanism: allowlist match against a real roster, multi-entity, safe.

Reproduces the mark-paid failure and shows the base class handles it, and that a subclass only supplies the
roster. Hermetic: uses a fake roster so it does not depend on the live DB (which is empty under test)."""
from mongoose.services.entity_matcher import Entity, EntityMatcher, tokens_for


class _People(EntityMatcher):
    def roster(self):
        return [
            Entity("1ECF8", "James Jennings", tokens_for("James Jennings")),
            Entity("1ECF9", "Karen Kingsley", tokens_for("Karen Kingsley")),
            Entity("1ECF3", "Elena Everett",
                   tokens_for("Elena Everett")),
        ]


def test_reproduces_and_fixes_the_mark_paid_message():
    hits = _People().match(
        "jennings and kingsley rtm payments landed so can you mark them paid, generate receipts, "
        "and told me the splits to use for subaccounts")
    assert {e.id for e in hits} == {"1ECF8", "1ECF9"}          # both real people, command noise ignored


def test_a_message_with_no_roster_name_matches_nothing():
    assert _People().match("what is the weather like today") == []
    assert _People().match("mark them all paid please") == []   # no distinctive name -> no financial action


def test_single_name_resolves_and_match_one_is_safe():
    assert [e.id for e in _People().match("mark Everett as paid")] == ["1ECF3"]
    assert _People().match_one("mark Everett as paid").id == "1ECF3"


def test_match_one_returns_none_when_ambiguous():
    class _Ambiguous(EntityMatcher):
        def roster(self):
            return [Entity("a", "Alice Archer", ["Samir", "harlow"]),   # both share 'Samir'
                    Entity("b", "Tara Thornton", ["Samir"])]
    assert _Ambiguous().match_one("mark Samir paid") is None       # ambiguous -> refuse to guess


def test_tokens_are_distinctive_only():
    toks = tokens_for("Ronan O'Brien")
    assert "obrien" in toks and "ronan" in toks     # apostrophe normalised, so it matches "o'brien"
    # short/common words are dropped so an incidental word cannot trigger a match
    assert all(len(t) >= 5 for t in tokens_for("Ann Long and Pat Raw"))


def test_routine_matcher_resolves_the_fixed_roster():
    from mongoose.services.entity_matcher import RoutineMatcher
    m = RoutineMatcher()
    assert [e.id for e in m.match("can you start my physio routine")] == ["physio"]
    assert [e.id for e in m.match("do the breathing one")] == ["breathing"]
    assert m.match("just chatting, no routine here") == []      # 'routine' is not a routine NAME


def test_payee_corp_suffix_is_not_distinctive():
    # a fabricated payee roster to prove 'Ltd' can never be the thing that matches
    from mongoose.services.entity_matcher import tokens_for, _CORP_STOP
    toks = [t for t in tokens_for("Lift Inspection Ltd", min_len=3) if t not in _CORP_STOP]
    assert "ltd" not in toks and "lift" in toks and "inspection" in toks


class _ShortNames(EntityMatcher):
    min_token_len = 3
    def roster(self):
        return [Entity("kit", "Kit", tokens_for("Kit", min_len=3)),
                Entity("sam", "Sam", tokens_for("Sam", min_len=3))]


def test_short_names_are_word_safe_not_substring():
    m = _ShortNames()
    assert [e.id for e in m.match("tell Kit the parcel arrived")] == ["kit"]      # the word 'kit'
    assert m.match("the parcel is in the kitchen") == []                          # 'kitchen' is NOT 'kit'
    assert m.match("i skittled nobody, promise") == []                            # 'skittled' is NOT 'kit'
    assert [e.id for e in m.match("kit's package")] == ["kit"]                    # possessive resolves
