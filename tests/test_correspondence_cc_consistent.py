"""Every outbound path must copy the SAME people.

Reported by leaseholders: Becker's managing agent was copied on his demand and dropped from his receipt.
Two root causes, and the first is the interesting one.

1. ONE FIELD, TWO MEANINGS. leaseholder_email_alt1 held "another address for the same person" for some
   flats (Rahimi's second gmail, Petrov's two spares) and "a different party who should be copied" for
   others. Any subsystem treating alt as CC would mail Petrov three times; any treating it as a fallback
   would never copy Becker's agent. Each subsystem guessed, and they guessed differently.

2. THE CC COLUMN WAS NAMED FOR ONE DOCUMENT TYPE. `demand_cc` was read by the demand mailer and unknown to
   the receipt sender. An agent is a party to the CORRESPONDENCE, not to one kind of letter.
"""
import pytest

from mongoose.services import rtm_management as M


@pytest.fixture()
def flats(tmp_path, monkeypatch):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "t.sqlite3"))
    monkeypatch.setenv("MONGOOSE_DATA_DIR", str(tmp_path))
    M._ensure_tables()
    # An agent who must be copied.
    _mk("1EC", 2, "Alexander Becker", "leaseholder2@example.com",
        alt1="agent@lettings.example", cc="agent@lettings.example")
    # Same human, second address: must NOT be copied.
    _mk("1EC", 5, "Benjamin Rahimi", "leaseholder5@example.com", alt1="leaseholder5.alt@example.com")
    return True


def _mk(block, num, name, email, alt1="", alt2="", cc=""):
    """upsert_flat does not take email columns, so set them directly (as the live rows were)."""
    M.upsert_flat(block=block, flat_number=num, leaseholder_name=name, payment_reference=name.split()[-1].upper())
    from mongoose.db import connect
    with connect() as con:
        con.execute("UPDATE rtm_mgmt_flats SET leaseholder_email=?, leaseholder_email_alt1=?, "
                    "leaseholder_email_alt2=?, demand_cc=? WHERE flat_id=?",
                    (email, alt1, alt2, cc, M._flat_id(block, num)))
        con.commit()


def test_the_agent_is_copied(flats):
    assert M.correspondence_cc("1ECF2") == ["agent@lettings.example"]


def test_an_address_is_never_listed_twice(flats):
    """Becker's agent sits in BOTH demand_cc and alt1 on the live row, so de-duplication is the thing
    that stops the same recipient appearing twice in the CC line."""
    assert M.correspondence_cc("1ECF2").count("agent@lettings.example") == 1


def test_all_three_of_petrovs_addresses_are_reachable(flats):
    _mk("1EC", 6, "Ivan Petrov", "leaseholder6@example.com",
        alt1="spare1@example.com", alt2="spare2@example.com")
    cc = M.correspondence_cc("1ECF6")
    assert cc == ["spare1@example.com", "spare2@example.com"]


def test_every_address_on_file_is_used(flats):
    """Owner's rule: primary in To, EVERY other address on file in CC.

    Before this, alt1/alt2 were inert: Petrov had three confirmed addresses and received everything at one,
    because nothing sent to them, nothing fell back to them, and nothing used them to recognise him inbound.
    """
    assert M.correspondence_cc("1ECF5") == ["leaseholder5.alt@example.com"]


def test_the_primary_recipient_is_never_also_in_cc(flats):
    """A duplicate in the CC line reads as carelessness and can double-deliver on some clients."""
    _mk("1EC", 7, "Self Cc", "dup@example.com", cc="dup@example.com")
    assert M.correspondence_cc("1ECF7") == []


def test_multiple_ccs_and_odd_separators_are_handled(flats):
    _mk("1EC", 8, "Two Agents", "owner@example.com", cc="a@agent.com; b@agent.com ,, ")
    assert M.correspondence_cc("1ECF8") == ["a@agent.com", "b@agent.com"]


def test_garbage_is_dropped_rather_than_mailed(flats):
    _mk("1EC", 9, "Bad Cc", "owner9@example.com", cc="not-an-email, ok@agent.com")
    assert M.correspondence_cc("1ECF9") == ["ok@agent.com"]


def test_a_flat_with_no_cc_gets_an_empty_list_not_a_crash(flats):
    assert M.correspondence_cc("1ECF999") == []


def test_the_demand_mailer_uses_the_shared_resolver(flats):
    """Structural: the mailer must not read the column itself, or it can drift from the other paths again."""
    import inspect
    from mongoose.services import rtm_demand_mailing as B
    src = inspect.getsource(B)
    assert 'get("demand_cc"' not in src and "get('demand_cc'" not in src, \
        "the mailer must go through correspondence_cc(), not read demand_cc directly"
    assert "correspondence_cc(" in src


def test_the_receipt_sender_copies_the_cc(flats):
    """The reported failure: this path used to copy nobody at all."""
    import inspect
    from mongoose.services import rtm_portal as P
    src = inspect.getsource(P.send_receipt_email)
    assert "correspondence_cc" in src, "receipts must copy the same parties as demands"
    assert '"cc"' in src, "the cc must actually reach the send payload"


def test_no_subsystem_reads_the_cc_column_directly():
    """One resolver, or the subsystems drift apart again. This is the guarantee, not the individual fixes."""
    import inspect
    from mongoose.services import rtm_demand_mailing, rtm_portal
    for mod in (rtm_demand_mailing, rtm_portal):
        src = inspect.getsource(mod)
        assert 'get("demand_cc"' not in src and "get('demand_cc'" not in src, (
            f"{mod.__name__} reads demand_cc directly; it must call rtm_management.correspondence_cc()")
