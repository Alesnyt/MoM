from backend import config
from backend.keys import chat_models_for_check


def test_startup_checks_only_saved_model(monkeypatch) -> None:
    monkeypatch.setattr(config, "OPENAI_MODEL", "qwen3.7-plus")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-sp-test")
    assert chat_models_for_check(thorough=False) == ["qwen3.7-plus"]


def test_manual_check_tries_other_qwen_models(monkeypatch) -> None:
    monkeypatch.setattr(config, "OPENAI_MODEL", "qwen3.7-plus")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-sp-test")
    models = chat_models_for_check(thorough=True)
    assert models[0] == "qwen3.7-plus"
    assert "qwen-turbo" in models
    assert len(models) > 1
