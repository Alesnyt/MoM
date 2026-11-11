from backend import config


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


def test_verified_status_is_bound_to_model_and_endpoint(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock
    from backend import keys

    monkeypatch.setattr(keys, "_status", {})
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-or-v1-" + "x" * 24)
    monkeypatch.setattr(config, "OPENAI_BASE_URL", config.OPENROUTER_BASE)
    monkeypatch.setattr(config, "OPENAI_MODEL", "openai/gpt-4o-mini")
    ping = AsyncMock()
    monkeypatch.setattr(keys, "_ping", ping)
    assert asyncio.run(keys.verify_key())["connected"]
    ping.assert_awaited_once()
    assert ping.await_args.args[1] == "openai/gpt-4o-mini"

    monkeypatch.setattr(config, "OPENAI_MODEL", "another-model")
    assert keys.snapshot()["chat_model"] == "another-model"
    assert not keys.snapshot()["connected"]
    assert keys.snapshot()["checked_at"] is None

    monkeypatch.setattr(config, "OPENAI_MODEL", "openai/gpt-4o-mini")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://private.example.test/v1")
    assert not keys.snapshot()["connected"]


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


def test_unavailable_qwen_model_does_not_change_saved_settings(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from openai import NotFoundError
    from backend import keys

    monkeypatch.setattr(keys, "_status", {})
    monkeypatch.setattr(config, "OPENAI_MODEL", "selected-qwen")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-sp-test")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", config.QWEN_BASE_INTL)
    response = SimpleNamespace(status_code=404, headers={}, request=None)
    ping = AsyncMock(side_effect=NotFoundError("model not found", response=response, body=None))
    monkeypatch.setattr(keys, "_ping", ping)

    status = asyncio.run(keys.verify_key())
    assert status["denied_models"] == ["selected-qwen"]
    assert not status["connected"]
    assert config.get_chat_model() == "selected-qwen"
    assert config.get_base_url() == config.QWEN_BASE_INTL
    assert [call.args[1] for call in ping.await_args_list] == ["selected-qwen"]
