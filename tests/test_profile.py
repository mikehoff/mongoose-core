"""The site profile: one private file of per-install identity, bound to the brain, never mixed with fiction."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from mongoose import profile as P


def _default() -> dict:
    return json.loads(P.PACKAGED_PROFILE.read_text())


def _write(tmp_path: Path, data: dict, name: str = "profile.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    p.chmod(0o600)
    return p


def test_default_profile_is_valid_and_fictional():
    P.reset_cache()
    assert P.is_default()
    assert P.profile_status()["ok"]
    assert P.site_domain().endswith((".example", "example-court.org", ".org"))
    assert P.block_codes() == ["1EC", "3EC"]


def test_missing_or_unknown_keys_fail_loud():
    bad = _default()
    del bad["site"]["domain"]
    bad["site"]["typo_domian"] = "x"
    with pytest.raises(P.ProfileError) as e:
        P.validate(bad)
    assert "missing site.domain" in str(e.value) and "typo_domian" in str(e.value)


def test_bad_block_code_rejected():
    bad = _default()
    bad["rtm"]["blocks"][0]["code"] = "one"
    with pytest.raises(P.ProfileError):
        P.validate(bad)


def test_profile_next_to_brain_wins(tmp_path, monkeypatch):
    data = _default()
    data["site"]["domain"] = "other.example"
    _write(Path(os.environ["MONGOOSE_DB_PATH"]).parent, data)
    P.reset_cache()
    try:
        assert not P.is_default()
        assert P.site_domain() == "other.example"
    finally:
        (Path(os.environ["MONGOOSE_DB_PATH"]).parent / "profile.json").unlink()
        P.reset_cache()


def test_explicit_override_and_cache_follows_file_changes(tmp_path, monkeypatch):
    data = _default()
    p = _write(tmp_path, data, "mine.json")
    monkeypatch.setenv("MONGOOSE_PROFILE_FILE", str(p))
    P.reset_cache()
    assert P.owner_name() == data["owner"]["name"]
    data["owner"]["name"] = "Robin"
    p.write_text(json.dumps(data))
    os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
    assert P.owner_name() == "Robin"


def test_dotenv_cannot_repoint_the_profile():
    from mongoose import settings as S
    assert "MONGOOSE_PROFILE_FILE" in S._BRAIN_LOCATION_KEYS


def test_loose_permissions_are_reported(tmp_path, monkeypatch):
    p = _write(tmp_path, _default(), "open.json")
    p.chmod(0o644)
    monkeypatch.setenv("MONGOOSE_PROFILE_FILE", str(p))
    P.reset_cache()
    st = P.profile_status()
    assert st["ok"] and st["private"] is False and "chmod 600" in st["warning"]


def test_render_fills_placeholders_and_refuses_unknown():
    P.reset_cache()
    out = P.render("Mail {{site.rtm_email}} or see {{wordpress.rest}}/health on {{site.domain}}")
    assert "{{" not in out and P.site_domain() in out and P.wp_namespace() in out
    with pytest.raises(P.ProfileError):
        P.render("{{site.nope}}")
    with pytest.raises(P.ProfileError):
        P.render("{{rtm.blocks}}")


def test_block_scheme_parsing_is_generated_from_the_profile():
    P.reset_cache()
    rx = re.compile(P.loose_flat_regex(), re.I)
    for s, want in [("1EC F2", ("1", "2")), ("1 ec f 2", ("1", "2")), ("flat 3ECF9", ("3", "9"))]:
        assert rx.search(s).groups() == want
    assert rx.search("2ECF1") is None
    assert P.block_for("1 Example Road") == "1EC" and P.block_for("3") == "3EC" and P.block_for("9") is None
    assert P.flat_id("1", 2) == "1ECF2"
    assert re.fullmatch(P.flat_id_regex(), "3ECF9") and not re.fullmatch(P.flat_id_regex(), "6ARF1")
    assert P.wp_route("/sync/snapshot") == f"/wp-json/{P.wp_namespace()}/sync/snapshot"
