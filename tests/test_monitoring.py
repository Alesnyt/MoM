import time
from collections import defaultdict, deque
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend import config, main, monitoring, performance, store


def test_monitoring_requires_admin_and_separates_processing_from_review(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(config, "WHISPER_DIR", tmp_path / "whisper")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "mom.db")
    monkeypatch.setattr(store, "get_max_jobs", lambda: 3)
    monkeypatch.setattr(monitoring, "resources", monitoring.ResourceSampler())
    monkeypatch.setattr(monitoring, "_process_totals", lambda: (0.0, 200, 1))
    monkeypatch.setattr(performance, "_timings", defaultdict(deque))
    monkeypatch.setattr(performance, "_outcomes", deque())
    performance.record_timing("protocol", 2)
    performance.record_timing("protocol", 6)
    performance.record_outcome("error")
    performance.record_outcome("done")
    store.init_db()
    owner = store.create_user("user@example.com", "unused")
    now = time.time()
    store.put_session("user-token", "user", user_id=owner["id"], email=owner["email"], expires=now + 3600)
    store.put_session("admin-token", "admin", username="admin", expires=now + 3600)
    statuses = ("extracting", "transcribing", "queued", "review", "done", "error")
    for status in statuses:
        store.create_meeting(status, "Частное название", "recording.wav", owner["id"])
        store.update_meeting(status, status=status, progress=35, transcript="Закрытый транскрипт")

    client = TestClient(main.app)
    path = "/api/admin/monitoring"
    assert client.get(path).status_code == 401
    client.cookies.set("mom_user", "user-token")
    assert client.get(path).status_code == 401
    client.cookies.set("mom_admin", "admin-token")
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["queue"].pop("oldest_wait_seconds") >= 0
    assert data["queue"] == {
        "active": 2, "waiting": 1, "review": 1, "limit": 3,
        "jobs": [{"id": status, "status": status, "progress": 35} for status in statuses[:2]],
    }
    assert data["resources"]["rss_bytes"] > 0
    assert data["disk"]["total_bytes"] > 0
    assert data["performance"] == {
        "stages": {"protocol": {"samples": 2, "median_seconds": 4, "p95_seconds": 6}},
        "finished": 2, "errors": 1,
    }
    assert "Частное название" not in response.text and "Закрытый транскрипт" not in response.text


def test_cpu_sampling_needs_a_baseline_and_can_exceed_one_core(monkeypatch):
    now = 100.0
    samples = iter([(12.0, 100, 1), (19.5, 200, 2), (22.0, 100, 1), (21.0, 100, 1)])
    monkeypatch.setattr(monitoring, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(monitoring, "_process_totals", lambda: next(samples))
    sampler = monitoring.ResourceSampler()

    assert sampler.snapshot() == {"cpu_percent": None, "rss_bytes": 100, "process_count": 1}
    now = 100.5
    assert sampler.snapshot()["cpu_percent"] is None
    now = 105.0
    assert sampler.snapshot() == {"cpu_percent": 150.0, "rss_bytes": 200, "process_count": 2}
    now = 130.0
    assert sampler.snapshot()["cpu_percent"] is None
    now = 135.0
    assert sampler.snapshot()["cpu_percent"] is None

    def denied():
        raise PermissionError("Process listing unavailable")

    monkeypatch.setattr(monitoring, "_process_totals", denied)
    now = 140.0
    assert sampler.snapshot() == {"cpu_percent": None, "rss_bytes": None, "process_count": None}
