"""Persistent settings survive Docker bootstrap, failed saves and restarts."""
import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys

import pytest
from dotenv import dotenv_values

from backend import config


@pytest.mark.parametrize("stage", ["write", "fsync", "replace"])
def test_failed_atomic_save_preserves_disk_and_running_settings(tmp_path, monkeypatch, stage):
    target = tmp_path / ".env"
    before = b"# original\nUI_THEME=classic\nSMTP_PASSWORD='keep$literal'\n"
    target.write_bytes(before)
    monkeypatch.setattr(config, "ENV_PATH", target)
    monkeypatch.setattr(config, "UI_THEME", "classic")
    monkeypatch.setenv("UI_THEME", "classic")

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "Test disk full")

    if stage == "write":
        real_fdopen = config.os.fdopen

        class PartialWrite:
            def __init__(self, *args, **kwargs):
                self.stream = real_fdopen(*args, **kwargs)

            def __enter__(self):
                return self

            def write(self, text):
                self.stream.write(text[:8])
                self.stream.flush()
                fail()

            def __exit__(self, *args):
                self.stream.close()

        monkeypatch.setattr(config.os, "fdopen", PartialWrite)
    else:
        monkeypatch.setattr(config.os, stage, fail)

    with pytest.raises(config.SettingsSaveError):
        config.set_ui_theme("t2")
    assert target.read_bytes() == before
    assert config.get_ui_theme() == os.environ["UI_THEME"] == "classic"
    assert list(tmp_path.glob(".env-*")) == []


@pytest.mark.parametrize("error_number", [errno.EBUSY, errno.EXDEV, errno.EACCES, errno.EPERM])
def test_replace_failure_never_falls_back_to_overwriting_live_file(tmp_path, monkeypatch, error_number):
    target = tmp_path / ".env"
    target.write_text("SMTP_PASSWORD='previous'\n")
    monkeypatch.setattr(config, "ENV_PATH", target)

    def fail(*args, **kwargs):
        raise OSError(error_number, "Test replacement failure")

    monkeypatch.setattr(config.os, "replace", fail)
    with pytest.raises(OSError):
        config._upsert_env({"SMTP_PASSWORD": "replacement"})
    assert target.read_text() == "SMTP_PASSWORD='previous'\n"
    assert list(tmp_path.glob(".env-*")) == []


def test_persistent_bootstrap_is_once_only_and_concurrent_saves_survive(tmp_path, monkeypatch):
    source = tmp_path / "bootstrap.env"
    source.write_text("# preserve comment\nSMTP_PASSWORD='a$literal'\nUI_THEME=classic\n")
    source.chmod(0o400)
    original = source.read_bytes()
    target = tmp_path / "data" / "config" / ".env"
    monkeypatch.setattr(config, "ENV_PATH", target)
    config.initialize_env(source)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: config._upsert_env({f"KEY_{i}": str(i)}), range(12)))
    config._upsert_env({"UI_THEME": "t2"})
    config.initialize_env(source)
    expected = {"SMTP_PASSWORD": "a$literal", "UI_THEME": "t2", **{f"KEY_{i}": str(i) for i in range(12)}}
    assert dotenv_values(target, interpolate=False) == expected
    assert source.read_bytes() == original
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700


def test_failed_bootstrap_is_retryable_and_preserves_source(tmp_path, monkeypatch):
    source = tmp_path / "bootstrap.env"
    original = "SMTP_PASSWORD='a$literal'\n"
    source.write_text(original)
    target = tmp_path / "data" / "config" / ".env"
    monkeypatch.setattr(config, "ENV_PATH", target)
    real_replace = config.os.replace

    def fail(*args, **kwargs):
        raise OSError(errno.ENOSPC, "Test disk full")

    monkeypatch.setattr(config.os, "replace", fail)
    with pytest.raises(OSError):
        config.initialize_env(source)
    assert not target.exists()
    assert source.read_text() == original
    assert list(target.parent.iterdir()) == []
    monkeypatch.setattr(config.os, "replace", real_replace)
    config.initialize_env(source)
    assert target.read_text() == original


def _python(code, cwd, env):
    return subprocess.check_output([sys.executable, "-c", code], cwd=cwd, env=env, text=True)


def test_actual_entrypoint_bootstrap_and_fresh_process_reload(tmp_path):
    # Execute the Python bootstrap from the shipped entrypoint, without Docker.
    # CI also tests the real container, mounted read-only source and recreation.
    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / ".env"
    original = "UI_THEME=classic\nSMTP_PASSWORD='bootstrap$literal'\nSETUP_TOKEN=''\n"
    source.write_text(original)
    source.chmod(0o400)
    target = tmp_path / "data" / "config" / ".env"
    env = dict(os.environ, MOM_ENV_PATH=str(target), PYTHONPATH=str(repo))
    for key in ("UI_THEME", "SMTP_PASSWORD", "SETUP_TOKEN"):
        env.pop(key, None)
    script = (repo / "deploy" / "docker-entrypoint.sh").read_text()
    bootstrap = script.split("python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    _python(bootstrap, tmp_path, env)
    first = dotenv_values(target, interpolate=False)
    assert first["SETUP_TOKEN"]
    assert first["SMTP_PASSWORD"] == "bootstrap$literal"
    _python("from backend import config; config.set_ui_theme('t2')", tmp_path, env)
    # Recreating the process must neither copy the old theme nor regenerate token.
    _python(bootstrap, tmp_path, env)
    snapshot = "from backend import config; import json; print(json.dumps([config.get_ui_theme(), config.get_smtp_password(), config.get_setup_token()]))"
    assert json.loads(_python(snapshot, tmp_path, env)) == ["t2", "bootstrap$literal", first["SETUP_TOKEN"]]
    assert source.read_text() == original
    # Deployment-provided process environment keeps the existing override policy.
    assert json.loads(_python(snapshot, tmp_path, dict(env, UI_THEME="classic")))[0] == "classic"
