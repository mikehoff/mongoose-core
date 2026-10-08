"""A salutation is written by the director, not derived by code.

`greeting_name` holds a FINISHED form of address, one per flat, chosen deliberately:

    'Dr Morgan'   'Dr Petrov'   'Martin and Carol Barnes'   'Brett and Karen Hollis'

The only correct operation on it is to print it. Three wrong salutations were produced in one sitting by
treating it as raw data to parse:

    'Dear Dr,'         first_name_from() took the title as the first name
    'Dear Morgan,'    a "fix" that parsed a field which must not be parsed
    'Dear 3ECF3,'      (earlier) fell through to the flat id when no name was found

The first of those was NOT new: first_name_from() feeds the demand letters and the payment-plan letters,
so two real leaseholders with titles had been addressed as 'Dear Dr' by every surface that used it.
"""
import pytest

from mongoose.services import rtm_templates as TPL


@pytest.mark.parametrize("name,expected", [
    ("Dr Alex James Morgan", "Alex"),
    ("Dr Ivan Petrov", "Ivan"),
    ("Prof Ada Lovelace", "Ada"),
    ("Mr. John Smith", "John"),
    ("Kathryn Hale", "Kathryn"),
])
def test_titles_are_not_mistaken_for_first_names(name, expected):
    assert TPL.first_name_from(name) == expected


@pytest.mark.parametrize("name", ["Mr & Mrs Smith", "Brett and Karen", "Martin and Carol Barnes"])
def test_joint_holders_are_never_split(name):
    """Halving a joint salutation addresses one leaseholder and ignores the other."""
    assert TPL.first_name_from(name) == name


def test_a_title_only_name_does_not_produce_an_empty_greeting():
    """Better a slightly odd salutation than 'Dear ,' on a real letter."""
    assert TPL.first_name_from("Dr") == "Dr"


def test_the_chaser_prints_the_salutation_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    from mongoose.services import rtm_management as M
    from mongoose.services import rtm_overdue as O
    from mongoose.db import connect
    M._ensure_tables()
    M.upsert_flat(block="1EC", flat_number=1, leaseholder_name="Dr Alex James Morgan",
                  payment_reference="MORGAN")
    M.create_demand(document_number="26-TO-DEC/1ECF1R0", flat="1ECF1", period="2026-H2",
                    amount="1080.21", issue_date="2026-07-16", due_date="2026-07-30")
    with connect() as con:
        con.execute("UPDATE rtm_mgmt_flats SET greeting_name='Dr Morgan' WHERE flat_id='1ECF1'")
        con.commit()
    body = O.render_chaser("1ECF1", today="2026-08-05")["body"]
    assert "Dear Dr Morgan," in body
    assert "Dear Morgan" not in body and "Dear Dr," not in body


def test_no_flat_on_the_live_roster_would_get_a_broken_salutation():
    """Sweep every real flat: a salutation must never be empty, a bare title, or a flat id."""
    from mongoose.services import rtm_management as M
    bad = []
    for f in M.list_flats():
        row = M._get_flat(f["flat_id"]) or {}
        greeting = (str(row.get("greeting_name") or "").strip()
                    or TPL.first_name_from(str(row.get("leaseholder_name") or "")))
        if (not greeting or greeting.lower().strip(".") in TPL._TITLES
                or greeting.upper() == f["flat_id"]):
            bad.append((f["flat_id"], greeting))
    assert not bad, f"flats that would be addressed wrongly: {bad}"
