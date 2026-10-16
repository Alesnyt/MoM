from backend import config
from backend.keys import chat_models_for_check


def test_chat_label_uses_the_saved_model(monkeypatch) -> None:
    monkeypatch.setattr(config, "OPENAI_MODEL", "MiniMax-M2")
    assert config.chat_label() == "Minimax"
    monkeypatch.setattr(config, "OPENAI_MODEL", "qwen3.7-plus")
    assert config.chat_label() == "Qwen"
    monkeypatch.setattr(config, "OPENAI_MODEL", "")
    assert config.chat_label("local-minimax") == "Minimax"


def test_gemini_key_selects_google_chat_and_local_asr(monkeypatch) -> None:
    for key in ("AIza" + "x" * 30, "AQ." + "x" * 30):
        monkeypatch.setattr(config, "OPENAI_API_KEY", key)
        monkeypatch.setattr(config, "OPENAI_BASE_URL", "")
        monkeypatch.setattr(config, "OPENAI_MODEL", "")
        assert config.detect_provider() == "gemini"
        assert config.provider_label() == "Gemini"
        assert config.default_base_url(key) == config.GEMINI_BASE
        assert config.default_chat_model() == "gemini-3.8-flash"
        assert config.default_asr_model() == config.LOCAL_ASR_MODEL
    assert config.chat_label("gemini-2.5-pro") == "Gemini"
    models = chat_models_for_check(thorough=True)
    assert models[0] == "gemini-3.8-flash"
    assert "gemini-2.5-flash" in models
    monkeypatch.setattr(config, "OPENAI_MODEL", "gpt-4o")
    assert chat_models_for_check(thorough=False) == ["gemini-3.8-flash"]


def test_openrouter_key_uses_router_and_local_asr(monkeypatch) -> None:
    key = "sk-or-v1-" + "x" * 24
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", config.GEMINI_BASE)
    monkeypatch.setattr(config, "OPENAI_MODEL", "gemini-3.8-flash")
    monkeypatch.setattr(config, "WHISPER_MODEL", "whisper-1")
    monkeypatch.setattr(config, "_upsert_env", lambda _values: None)
    config.set_openai_api_key(key)
    assert config.detect_provider() == "openrouter"
    assert config.provider_label() == "OpenRouter"
    assert config.get_base_url() == config.OPENROUTER_BASE
    assert config.get_chat_model() == "openai/gpt-4o-mini"
    assert config.get_asr_model() == config.LOCAL_ASR_MODEL
    assert config.chat_label() == "OpenAI"
    assert config.chat_label("anthropic/claude-sonnet-4") == "Claude"
    assert chat_models_for_check(thorough=False) == ["openai/gpt-4o-mini"]
    from backend.keys import chat_model_ids

    assert chat_model_ids(
        ["openai/gpt-4o-mini", "google/gemini-2.5-flash-image", "whisper-1", "meta-llama/llama-3.3-70b-instruct"],
        provider="openrouter",
    ) == ["openai/gpt-4o-mini", "meta-llama/llama-3.3-70b-instruct"]


def test_private_openai_stays_in_contour_and_clouds_use_their_own_hosts(monkeypatch) -> None:
    from backend.analyze import make_client

    monkeypatch.setattr(config, "_upsert_env", lambda _values: None)
    private = "https://llm.corp.local/v1"
    local_key = "sk-local-" + "x" * 24
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", private)
    monkeypatch.setattr(config, "OPENAI_MODEL", "qwen2.5-72b")
    monkeypatch.setattr(config, "WHISPER_MODEL", "gigaam-multilingual-large")

    config.set_openai_api_key(local_key)
    assert config.detect_provider() == "openai"
    assert config.provider_label() == "Локальный API"
    assert config.get_base_url() == private
    assert config.get_chat_model() == "qwen2.5-72b"
    assert config.get_asr_model() == "gigaam-multilingual-large"
    client = make_client(local_key)
    assert str(client.base_url).rstrip("/") == private
    extra = {str(key).lower() for key in (getattr(client, "_custom_headers", None) or {})}
    assert "x-goog-api-key" not in extra
    assert "http-referer" not in extra

    config.set_openai_api_key("sk-ws-" + "x" * 24)
    assert config.detect_provider() == "qwen"
    assert "dashscope" in config.get_base_url()
    assert config.get_chat_model().startswith("qwen")
    assert config.get_base_url() != private

    config.set_openai_api_key("AQ." + "x" * 30)
    assert config.detect_provider() == "gemini"
    assert config.get_base_url() == config.GEMINI_BASE
    assert config.get_chat_model().startswith("gemini")
    assert config.is_local_asr(config.get_asr_model())
    assert config.get_asr_model() == "gigaam-multilingual-large"

    config.set_openai_api_key("sk-or-v1-" + "x" * 24)
    assert config.detect_provider() == "openrouter"
    assert config.get_base_url() == config.OPENROUTER_BASE
    assert "/" in config.get_chat_model()

    monkeypatch.setattr(config, "OPENAI_BASE_URL", config.QWEN_BASE_INTL)
    monkeypatch.setattr(config, "OPENAI_MODEL", "qwen-plus")
    monkeypatch.setattr(config, "WHISPER_MODEL", "qwen3-asr-flash")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-ws-" + "x" * 24)
    config.set_openai_api_key(local_key)
    assert config.detect_provider() == "openai"
    assert config.get_base_url() == ""
    assert config.get_chat_model() == "gpt-4o"
    assert config.get_asr_model() == config.LOCAL_ASR_MODEL

    from backend.keys import chat_model_ids

    local_ids = chat_model_ids(
        ["qwen2.5-72b", "gpt-4o", "whisper-large", "text-embedding-3-large"],
        provider="openai",
        private=True,
    )
    assert local_ids == ["qwen2.5-72b", "gpt-4o"]
    public_ids = chat_model_ids(
        ["qwen2.5-72b", "gpt-4o", "whisper-large"],
        provider="openai",
        private=False,
    )
    assert public_ids == ["gpt-4o"]


def test_connected_status_shows_the_saved_model(monkeypatch) -> None:
    from backend import keys

    key = "sk-or-v1-" + "x" * 24
    previous = dict(keys._status)
    monkeypatch.setattr(config, "OPENAI_API_KEY", key)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", config.OPENROUTER_BASE)
    monkeypatch.setattr(config, "OPENAI_MODEL", "openai/gpt-4o-mini")
    keys._mark(
        connected=True,
        message="OpenRouter принял ключ. Модель: openai/gpt-4o-mini",
        checked_at="t",
        key=key,
    )
    monkeypatch.setattr(config, "OPENAI_MODEL", "nvidia/nemotron-3.5-super-vl:free")
    try:
        assert keys.snapshot()["message"] == "OpenRouter принял ключ. Модель: nvidia/nemotron-3.5-super-vl:free"
        assert keys.snapshot()["chat_model"] == "nvidia/nemotron-3.5-super-vl:free"
    finally:
        keys._status.clear()
        keys._status.update(previous)


def test_chat_catalog_keeps_text_models_only() -> None:
    from backend.keys import chat_model_ids

    ids = chat_model_ids(
        [
            "models/gemini-3.8-flash",
            "models/gemini-2.5-pro",
            "models/gemini-2.5-flash-image",
            "models/gemini-2.5-flash-native-audio-latest",
            "models/text-embedding-004",
            "qwen-plus",
            "qwen3-asr-flash",
            "gpt-4o",
            "whisper-1",
        ],
        provider="gemini",
    )
    assert ids == ["gemini-3.8-flash", "gemini-2.5-pro"]


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
