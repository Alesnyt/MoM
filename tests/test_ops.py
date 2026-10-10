from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest

from backend import auth, backup, config, disk, gigaam_asr, store


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data" / "mom.db"
    data = tmp_path / "data"
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(config, "UPLOAD_DIR", data / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", data / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", data / "whisper")
    monkeypatch.setattr(config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(store, "DB_PATH", path)
    store.init_db()
    return path


def test_disk_refuses_when_reserve_is_gone(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "MIN_FREE_BYTES", 2 * 1024 * 1024 * 1024)

    class _Usage:
        total = 10 * 1024 * 1024 * 1024
        used = 9 * 1024 * 1024 * 1024
        free = 800 * 1024 * 1024

    monkeypatch.setattr(disk, "_usage", lambda path: _Usage())
    disk._recordings_cache["at"] = 0
    (config.UPLOAD_DIR / "a.webm").write_bytes(b"12345")
    current = disk.snapshot()
    assert current["ok"] is False
    assert current["recordings_bytes"] == 5
    with pytest.raises(disk.DiskError, match="свободно"):
        disk.ensure_space()
    with pytest.raises(disk.DiskError, match="не помещается"):
        disk.ensure_space(1024)

    def _walk() -> int:
        raise AssertionError("public disk check walked recordings")

    monkeypatch.setattr(disk, "_recordings_bytes", _walk)
    assert disk.has_room() is False
    with pytest.raises(disk.DiskError):
        disk.ensure_space()


def test_snapshot_roundtrip_and_rejects_data_dir(db: Path) -> None:
    user = store.create_user("a@example.com", auth.hash_password("secret-pass"), 5)
    store.create_meeting("meet1", "Планёрка", "plan.webm", user["id"])
    (config.UPLOAD_DIR / "meet1.webm").write_bytes(b"audio")
    (config.DATA_DIR / "hf").mkdir(exist_ok=True)
    (config.DATA_DIR / "hf" / "weight.bin").write_bytes(b"model")

    folder = backup.create_snapshot()
    checked = backup.verify_snapshot(folder)
    assert checked["ok"] is True
    assert checked["meetings"] == 1
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "mom.db").stat().st_mode & 0o777 == 0o600
    assert (folder / "uploads" / "meet1.webm").stat().st_mode & 0o777 == 0o600
    assert (folder / "uploads" / "meet1.webm").read_bytes() == b"audio"
    assert not (folder / "hf").exists()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert "hf" in manifest["omits"]
    assert backup.main(["--check", str(folder)]) == 0

    with pytest.raises(backup.BackupError, match="внутрь data"):
        backup.create_snapshot(config.DATA_DIR / "nested")

    database = folder / "mom.db"
    database.write_bytes(database.read_bytes()[:32])
    broken = backup.verify_snapshot(folder)
    assert broken["ok"] is False
    assert backup.main(["--check", str(folder)]) == 1


def test_gigaam_code_is_pinned_and_not_remote() -> None:
    assert gigaam_asr.vendor_code_sha256() == config.GIGAAM_CODE_SHA256
    pin = gigaam_asr.gigaam_pin("large_ctc")
    assert pin["commit"] == config.GIGAAM_PINS["large_ctc"]["commit"]
    assert len(pin["weights_sha256"]) == 64
    assert len(pin["config_sha256"]) == 64
    source = Path(gigaam_asr.__file__).read_text()
    assert "trust_remote_code=True" not in source
    assert "trust_remote_code=False" in source


def test_gigaam_load_checks_weight_size(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sys
    import types

    import hashlib

    calls: list[dict] = []
    expected = config.GIGAAM_PINS["ctc"]["weights_size"]
    config_text = json.dumps({"auto_map": {"AutoModel": "remote.Code"}, "cfg": {"ok": True}})
    config_digest = hashlib.sha256(config_text.encode()).hexdigest()
    sizes = {"pytorch_model.bin": 4}

    def fake_download(**kwargs):
        path = tmp_path / kwargs["filename"]
        if kwargs["filename"] == "config.json":
            path.write_text(config_text)
        else:
            path.write_bytes(b"x")
        return str(path)

    class _Config:
        def __init__(self, cfg=None):
            self.cfg = cfg

    class _Model:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            calls.append(kwargs)
            return "loaded"

    module = types.ModuleType("backend.vendor.gigaam.modeling_gigaam")
    module.GigaAMConfig = _Config
    module.GigaAMModel = _Model
    hub = types.ModuleType("huggingface_hub")
    hub.hf_hub_download = fake_download
    monkeypatch.setitem(sys.modules, "backend.vendor.gigaam.modeling_gigaam", module)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    real_stat = Path.stat

    def sized_stat(self: Path):
        if self.name in sizes:
            return type("S", (), {"st_size": sizes[self.name]})()
        return real_stat(self)

    monkeypatch.setattr(Path, "stat", sized_stat)

    with pytest.raises(RuntimeError, match="Размер веса"):
        gigaam_asr.load_gigaam_model("ctc")

    sizes["pytorch_model.bin"] = expected
    with pytest.raises(RuntimeError, match="config.json"):
        gigaam_asr.load_gigaam_model("ctc")
    monkeypatch.setitem(config.GIGAAM_PINS["ctc"], "config_sha256", config_digest)
    loaded = gigaam_asr.load_gigaam_model("ctc")
    assert loaded == "loaded"
    assert calls[0]["trust_remote_code"] is False
    assert calls[0]["revision"] == config.GIGAAM_PINS["ctc"]["commit"]
    assert calls[0]["config"].cfg == {"ok": True}


def test_delete_user_removes_meetings_and_blocks_active(db: Path) -> None:
    user = store.create_user("gone@example.com", auth.hash_password("secret-pass"), 5)
    store.create_meeting("meet-gone", "Черновик", "a.webm", user["id"])
    store.put_session("tok", "user", user_id=user["id"], email=user["email"], expires=time.time() + 60)
    removed = store.delete_user(user["id"])
    assert removed == ["meet-gone"]
    assert store.get_user(user["id"]) is None
    assert store.get_meeting("meet-gone") is None
    assert store.get_session_row("tok") is None

    busy = store.create_user("busy@example.com", auth.hash_password("secret-pass"), 5)
    store.create_meeting("meet-busy", "В работе", "b.webm", busy["id"])
    store.update_meeting("meet-busy", status="transcribing")
    with pytest.raises(ValueError):
        store.delete_user(busy["id"])
    assert store.get_user(busy["id"]) is not None


def test_session_token_is_hashed(db: Path) -> None:
    raw = "cookie-token-not-stored"
    store.put_session(raw, "user", user_id="u1", email="a@example.com", expires=time.time() + 60)
    row = store.get_session_row(raw)
    assert row is not None
    assert row["token"] == hashlib.sha256(raw.encode()).hexdigest()
    assert row["email"] == "a@example.com"
    store.drop_session_token(raw)
    assert store.get_session_row(raw) is None


def test_public_health_hides_models_and_disk_numbers(db: Path) -> None:
    from backend.main import _health

    public = _health(full=False)
    assert public["disk"] == {"ok": public["disk"]["ok"]}
    assert public["openai"]["chat_model"] is None
    assert public["openai"]["asr_model"] is None
    assert public["openai"]["base_url"] is None
    assert public["openai"]["hint"] is None
    assert public["openai"]["denied_models"] == []
    full = _health(full=True)
    assert "free_bytes" in full["disk"]
