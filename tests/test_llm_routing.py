from mongoose.settings import get_settings


def test_role_maps_to_distinct_models(monkeypatch):
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "fake-chat-model")
    monkeypatch.setenv("OLLAMA_TOOL_MODEL", "fake-tool-model")
    monkeypatch.setenv("OLLAMA_CODE_MODEL", "fake-code-model")
    s = get_settings()
    assert s.model_for_role("chat") == "fake-chat-model"
    assert s.model_for_role("tool") == "fake-tool-model"
    assert s.model_for_role("code") == "fake-code-model"


def test_unknown_role_falls_back_to_chat(monkeypatch):
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "fake-chat-model")
    s = get_settings()
    assert s.model_for_role("nonsense") == "fake-chat-model"


def test_legacy_ollama_model_defaults_to_chat(monkeypatch):
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("OLLAMA_CHAT_MODEL", "fake-chat-model")
    s = get_settings()
    assert s.ollama_model == "fake-chat-model"
