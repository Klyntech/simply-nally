from nally import config as cfg


def test_defaults():
    assert cfg.PROVIDER in ("openai", "groq", "opencode")
    assert cfg.MODEL
    assert cfg.BASE_URL.startswith("http")
    assert cfg.MAX_ITERATIONS >= 1
    assert cfg.MAX_TOOL_CALLS >= 1
    assert cfg.get_system_prompt()


def test_validate_missing_key(monkeypatch):
    # Simulate missing key
    monkeypatch.setattr(cfg, "API_KEY", "")
    errors = cfg.validate_config(require_api_key=True)
    assert errors
    assert any("Missing API key" in e for e in errors)


def test_validate_ok(monkeypatch):
    monkeypatch.setattr(cfg, "API_KEY", "sk-test")
    monkeypatch.setattr(cfg, "MAX_ITERATIONS", 5)
    monkeypatch.setattr(cfg, "MAX_TOOL_CALLS", 5)
    errors = cfg.validate_config(require_api_key=True)
    assert errors == []
