"""RTM knowledge base: loader mechanics (synthetic, no real data) + reconciliation against the engine
and the owner's own golden spec when the real base is installed."""
import subprocess
import sys

import pytest


def test_kb_loader_and_provenance(tmp_path, monkeypatch):
    root = tmp_path / "rtm_knowledge"
    (root / "legal").mkdir(parents=True)
    (root / "semantics.yaml").write_text("meta: {kind: semantics, version: 1}\ncompany: {type_rule: 'Always RTM, never RMC'}\n")
    (root / "legal" / "matters.yaml").write_text("meta: {kind: legal, layer: matters}\nftt: {hearing: 'mid-August'}\n")
    (root / "BRIEFING.md").write_text("orientation prose only")
    monkeypatch.setenv("MONGOOSE_RTM_KB_DIR", str(root))
    from mongoose.services import rtm_knowledge as K
    K.reset()
    assert K.is_installed() and K.kb_root() == root
    kb = K.load(force=True)
    assert "semantics.yaml" in kb and "legal/matters.yaml" in kb
    # BRIEFING is loaded but flagged NON-canonical, and excluded from grounded search
    assert kb["BRIEFING.md"]["canonical"] is False
    assert K.search("orientation") == []
    # section fetch by path and by kind/layer
    assert K.section("legal/matters.yaml")["ftt"]["hearing"] == "mid-August"
    assert K.section("legal/matters")["ftt"]["hearing"] == "mid-August"
    hits = K.search("mid-August")
    assert any("hearing" in h["path"] for h in hits)
    K.reset()


def test_installed_kb_reconciles_engine_and_golden():
    """When the real base is installed, the ontology audit (which now includes the KB<->engine
    reconciliation) must be green, and the owner's own golden.py spec must pass against it."""
    from mongoose.services import rtm_knowledge as K, rtm_ontology as O
    K.reset()
    O.reset()
    if not K.is_installed():
        pytest.skip("RTM knowledge base not installed at the canonical location")
    rep = O.ontology().audit()
    assert rep["failed"] == 0, [c for c in rep["checks"] if c["ok"] is False]
    # the reconciliation invariant must actually have RUN (not skipped) , the base agrees with the engine
    recon = [c for c in rep["checks"] if c["check"].startswith("invariant 'kb_reconciles_engine'")]
    assert recon and recon[0]["ok"] is True, recon
    # run the owner's authored golden spec against the installed base
    root = K.kb_root()
    gp = root / "tests" / "golden.py"
    if gp.exists():
        r = subprocess.run([sys.executable, str(gp)], capture_output=True, text=True, cwd=str(root))
        assert r.returncode == 0, (r.stdout[-1000:] + "\n" + r.stderr[-400:])
    K.reset()
    O.reset()
