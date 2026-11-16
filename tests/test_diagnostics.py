import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from backend import config, diagnostics as diag


@pytest.fixture(autouse=True)
def isolated_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(diag, "_active", {})
    monkeypatch.setattr(diag, "_environment", lambda: {"logical_cpus": 12, "affinity_cpus": 12,
                                                       "host_memory_total_bytes": 16 * 1024**3})
    monkeypatch.setattr(diag, "_resource_values", lambda: ({}, None, None))


def test_durable_summary_export_and_distinct_attempts():
    assert diag.initialize()
    audio = diag.begin_run("private-meeting-id", "audio", queue_wait_seconds=12)
    with diag.run_scope(audio):
        diag.record_stage("asr", 20)
        diag.record_stage("gigaam_model_load", 3)
    diag.finish_run(audio, status="review", audio_seconds=200)
    protocol = diag.begin_run("private-meeting-id", "protocol", queue_wait_seconds=2, audio_seconds=200)
    with diag.run_scope(protocol):
        diag.record_stage("protocol", 30)
        diag.record_llm("protocol", {"seconds": 30, "prompt_tokens": 8000, "completion_tokens": 1000,
                                     "total_tokens": 9000, "reasoning_tokens": 300, "cached_tokens": 100,
                                     "sdk_attempts": 2, "http_attempts": 2, "outcome": "ok", "finish_reason": "stop",
                                     "retry_reason": "none", "error_kind": "none", "ttft_seconds": None})
    diag.finish_run(protocol, status="done")
    diag.initialize()
    summary = diag.summary()
    assert (summary["runs"], summary["completed"], summary["active"], summary["errors"]) == (2, 2, 0, 0)
    assert summary["stages"]["asr"]["median_seconds"] == 20
    assert summary["stages"]["queue_wait"]["median_seconds"] == 7
    report = diag.export_report()
    assert report["available"] and report["exported_runs"] == 2
    assert report["runs"][0]["meeting"] == report["runs"][1]["meeting"]
    by_phase = {run["phase"]: run for run in report["runs"]}
    assert by_phase["audio"]["asr_rtf"] == .1
    assert by_phase["audio"]["model_loads"]["gigaam_model_load"] == 1
    assert by_phase["protocol"]["llm"][0]["sdk_attempts"] == 2
    assert by_phase["protocol"]["llm"][0]["ttft_seconds"] is None
    assert "private-meeting-id" not in json.dumps(report)
    # The collection database itself also never stores the raw meeting ID.
    assert b"private-meeting-id" not in diag._path().read_bytes()


def test_no_sensitive_free_text_even_in_model_names_and_injected_storage(monkeypatch):
    secret = "PrivatePerson-secret-https://corp.internal/API_TOKEN"
    monkeypatch.setattr(config, "get_chat_model", lambda: f"MiniMax-M3/{secret}")
    monkeypatch.setattr(config, "get_asr_model", lambda: secret)
    run = diag.begin_run(secret, "audio")
    with diag.run_scope(run):
        diag.record_stage(secret, 12)
        diag.record_llm("protocol", {"seconds": 3, "input_chars": secret, "response": secret,
                                      "prompt": secret, "exception": secret, "finish_reason": secret,
                                      "error_kind": secret, "outcome": "error", "http_status": 503})
    diag.finish_run(run, status="error")
    # Revalidate the persisted format at export, including future/unknown keys.
    with sqlite3.connect(diag._path()) as conn:
        payload = json.loads(conn.execute("SELECT payload FROM runs").fetchone()[0])
        payload.update(title=secret, transcript=secret, endpoint=secret, error=secret, app_version=secret)
        payload["settings"].update(llm_family=secret, llm_model_ref=secret, token=secret, max_jobs=secret)
        payload["environment"].update(hostname=secret, os=secret, python=secret)
        payload["resources"] = [{"hostname": secret, "process_rss_bytes": 42, "host_swap_used_bytes": secret}]
        payload["events"] += [{"stage": secret, "seconds": 4}, {"stage": "asr", "outcome": secret, "seconds": 2}]
        payload["llm"] += [{"phase": secret, "prompt": secret}, {"phase": "name_review", "completion_tokens": 22, "prompt": secret}]
        conn.execute("UPDATE runs SET payload = ?", (json.dumps(payload),))
    for data in (diag.export_report(), diag.summary()):
        assert secret not in json.dumps(data)
        assert "corp.internal" not in json.dumps(data)
    report = diag.export_report()
    exported = report["runs"][0]
    assert exported["settings"]["max_jobs"] is None
    assert exported["llm"][0]["input_chars"] is None
    assert exported["llm"][0]["finish_reason"] == "unknown"
    assert exported["llm"][0]["error_kind"] == "other"
    assert exported["resources"][0]["process_rss_bytes"] == 42


def test_flush_survives_restart_and_live_run_is_not_interrupted(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(diag, "time", SimpleNamespace(time=lambda: 1_800_000_000 + clock[0], monotonic=lambda: clock[0]))
    run = diag.begin_run("meeting", "audio")
    with diag.run_scope(run):
        diag.record_stage("audio_prepare", 2)
    clock[0] += 16
    diag.sample_run(run)
    assert diag.initialize()
    assert diag.export_report()["runs"][0]["status"] == "running"
    diag._active.clear()  # simulate a new process after a hard stop
    assert diag.initialize()
    exported = diag.export_report()["runs"][0]
    assert exported["status"] == "interrupted"
    assert exported["elapsed_is_partial"] is True
    assert exported["elapsed_seconds"] == 16
    assert exported["stages"]["audio_prepare"]["seconds"] == 2


def test_context_isolation_across_async_jobs_and_threads():
    first = diag.begin_run("one", "audio")
    second = diag.begin_run("two", "protocol")

    async def record(run, stage, seconds):
        with diag.run_scope(run):
            await asyncio.sleep(0)
            await asyncio.to_thread(diag.record_stage, stage, seconds)
            await asyncio.to_thread(diag.record_llm, "protocol", {"seconds": seconds, "input_chars": seconds})

    async def jobs():
        await asyncio.gather(record(first, "asr", 7), record(second, "protocol", 13))

    asyncio.run(jobs())
    diag.record_stage("asr", 900)  # outside any scope: ignored
    diag.finish_run(first, status="review")
    diag.finish_run(second, status="done")
    runs = {run["phase"]: run for run in diag.export_report()["runs"]}
    assert set(runs["audio"]["stages"]) == {"asr"}
    assert runs["audio"]["stages"]["asr"]["seconds"] == 7
    assert runs["protocol"]["stages"]["protocol"]["seconds"] == 13
    assert runs["audio"]["llm"][0]["input_chars"] == 7
    assert runs["protocol"]["llm"][0]["input_chars"] == 13


def test_bounded_events_and_timeline_keep_total_counts_and_peaks(monkeypatch):
    monkeypatch.setattr(diag, "MAX_EVENTS", 2)
    monkeypatch.setattr(diag, "MAX_LLM_CALLS", 2)
    monkeypatch.setattr(diag, "MAX_SAMPLES", 4)
    clock = [0.0]
    monkeypatch.setattr(diag, "time", SimpleNamespace(time=lambda: 1_800_000_000 + clock[0], monotonic=lambda: clock[0]))
    monkeypatch.setattr(diag, "_resource_values", lambda: ({"process_rss_bytes": int(clock[0] * 100),
        "host_available_memory_bytes": 10000 - int(clock[0] * 100)}, clock[0] * 2,
        (clock[0] * 10, clock[0], clock[0] * 2, clock[0] * 3)))
    run = diag.begin_run("one", "audio")
    with diag.run_scope(run):
        for _ in range(5):
            diag.record_stage("asr", 2)
            diag.record_llm("protocol", {"seconds": 1})
    for number in range(17):
        clock[0] = number * 2
        diag.sample_run(run)
    diag.finish_run(run, status="review")
    exported = diag.export_report()["runs"][0]
    assert exported["stages"]["asr"] == {"seconds": 10, "count": 5, "interrupted": 0}
    assert exported["dropped"]["events"] == 3
    assert exported["dropped"]["llm"] == 3
    assert exported["resource_sample_count"] == 17
    assert len(exported["resources"]) <= 4
    assert exported["peak_process_rss_bytes"] == 3200
    assert exported["min_host_available_memory_bytes"] == 6800
    assert exported["peak_process_cpu_percent"] == 200
    assert exported["resources"][-1]["host_cpu_steal_percent"] == 10
    assert exported["resources"][-1]["host_cpu_iowait_percent"] == 20
    assert exported["resources"][-1]["host_cpu_busy_percent"] == 50


def test_limits_retention_and_invalid_records(monkeypatch):
    diag.initialize()
    base = {"phase": "audio", "status": "review", "stages": {}, "resources": []}
    now = diag.time.time()
    with sqlite3.connect(diag._path()) as conn:
        for index in range(8):
            conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                         (str(index), str(index % 2), now - index, now, json.dumps(base)))
        conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                     ("old", "old", now - 20 * 86400, now, json.dumps(base)))
    monkeypatch.setattr(diag, "MAX_RUNS", 6)
    monkeypatch.setattr(diag, "MAX_EXPORT_RUNS", 3)
    report = diag.export_report(14)
    assert report["total_selected_runs"] == 6
    assert report["exported_runs"] == 3 and report["truncated"]
    assert len(diag.summary()["recent_runs"]) == 6
    with sqlite3.connect(diag._path()) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 6
        conn.execute("UPDATE runs SET payload = ? WHERE id = ?", ("{invalid json", "0"))
    report = diag.export_report()
    assert report["skipped_invalid_runs"] == 1
    assert report["exported_runs"] == 3


def test_export_byte_limit_and_storage_size_limit(monkeypatch):
    for i in range(4):
        run = diag.begin_run(str(i), "protocol")
        diag.finish_run(run, status="done")
    monkeypatch.setattr(diag, "MAX_REPORT_BYTES", 5000)
    report = diag.export_report()
    assert len(json.dumps(report, ensure_ascii=True, separators=(",", ":"))) <= 5000
    assert report["truncated"]
    monkeypatch.setattr(diag, "MAX_STORAGE_BYTES", 3000)
    diag.export_report()
    with sqlite3.connect(diag._path()) as conn:
        size = conn.execute("SELECT coalesce(sum(length(payload)),0) FROM runs").fetchone()[0]
    assert size <= 3000


def test_combined_detail_byte_budget_preserves_finished_run_and_counts():
    run = diag.begin_run("one", "audio", audio_seconds=600)
    run.data["resources"] = [{key: 10**18 for key in diag.RESOURCE_NUMBERS} for _ in range(diag.MAX_SAMPLES)]
    run.data["resource_sample_count"] = diag.MAX_SAMPLES
    run.data["peak_process_rss_bytes"] = 10**18
    run.data["llm"] = [{"phase": "protocol", **{key: 10**18 for key in diag.LLM_NUMBERS},
                        "outcome": "ok", "finish_reason": "stop", "retry_reason": "none", "error_kind": "none",
                        "offset_seconds": 10**18} for _ in range(diag.MAX_LLM_CALLS)]
    run.data["events"] = [{"stage": "asr", "seconds": 10**18, "offset_seconds": 10**18, "outcome": "ok"}
                           for _ in range(diag.MAX_EVENTS)]
    run.data["stages"] = {"asr": {"count": diag.MAX_EVENTS, "seconds": 10**18, "interrupted": 0}}
    assert len(diag._json(run.data)) > diag.MAX_PAYLOAD_BYTES
    assert diag.summary()["active"] == 1  # Active export uses the same byte bound.
    diag.finish_run(run, status="done")
    assert not diag._active
    assert diag.initialize()
    with sqlite3.connect(diag._path()) as conn:
        raw = conn.execute("SELECT payload FROM runs").fetchone()[0]
    assert len(raw) <= diag.MAX_PAYLOAD_BYTES
    exported = diag.export_report()["runs"][0]
    assert exported["status"] == "done"
    assert exported["peak_process_rss_bytes"] == 10**18
    assert exported["stages"]["asr"]["count"] == diag.MAX_EVENTS
    for key, total in (("resources", diag.MAX_SAMPLES), ("llm", diag.MAX_LLM_CALLS), ("events", diag.MAX_EVENTS)):
        assert len(exported[key]) + exported["dropped"][key] == total
    assert sum(exported["dropped"].values()) > 0


def test_waiting_sampler_persists_latest_terminal_state(monkeypatch):
    run = diag.begin_run("one", "audio")
    writer_lock = threading.Lock()
    waiting = threading.Event()

    class Gate:
        def __enter__(self):
            waiting.set()
            writer_lock.acquire()

        def __exit__(self, *args):
            writer_lock.release()

    monkeypatch.setattr(diag, "_storage_lock", Gate())
    writer_lock.acquire()
    writer = threading.Thread(target=diag._persist, args=(run,))
    writer.start()
    try:
        assert waiting.wait(2)
        with run.lock:
            run.data["status"] = "done"
            run.data["elapsed_seconds"] = 123
    finally:
        writer_lock.release()
        writer.join(timeout=2)
    assert not writer.is_alive()
    with sqlite3.connect(diag._path()) as conn:
        saved = json.loads(conn.execute("SELECT payload FROM runs").fetchone()[0])
    assert saved["status"] == "done" and saved["elapsed_seconds"] == 123


def test_corrupt_nested_json_and_nonfinite_derived_metric_do_not_break_export():
    diag.initialize()
    now = diag.time.time()
    valid = {"phase": "audio", "status": "review", "audio_seconds": 1e-320,
             "stages": {"asr": {"seconds": 10**18}}, "events": [{"stage": []}], "llm": [{"phase": {}}]}
    with sqlite3.connect(diag._path()) as conn:
        for name, payload in (("nested", "[" * 10000 + "0" + "]" * 10000), ("valid", json.dumps(valid))):
            conn.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)", (name, name, now, now, payload))
    report = diag.export_report()
    assert report["skipped_invalid_runs"] == 1
    assert report["exported_runs"] == 1
    assert report["runs"][0]["asr_rtf"] is None
    assert diag.summary()["runs"] == 1
    json.dumps(report, allow_nan=False)


def test_storage_failure_does_not_break_processing(monkeypatch):
    def fail():
        raise OSError("PRIVATE_PATH_OR_SECRET")

    monkeypatch.setattr(diag, "_connect", fail)
    assert diag.initialize() is False
    run = diag.begin_run("one", "audio")
    with diag.run_scope(run):
        diag.record_stage("asr", 3)
        diag.record_llm("protocol", {"seconds": 1})
    diag.sample_run(run)
    diag.finish_run(run, status="error")
    assert not diag._active
    report = diag.export_report()
    assert not report["available"]
    assert report["error"] == "storage_unavailable"
    assert "PRIVATE" not in json.dumps(report)


def test_corrupt_database_has_explicit_unavailable_status():
    diag._path().write_text("not a sqlite database")
    assert diag.summary()["error"] == "storage_unavailable"
    assert diag.export_report()["available"] is False


def test_upload_tracks_receive_and_save_without_private_metadata():
    diag.record_upload("private-id", receive_seconds=12, save_seconds=3, size_bytes=2048)
    run = diag.export_report()["runs"][0]
    assert run["phase"] == "upload" and run["status"] == "uploaded"
    assert run["size_bytes"] == 2048
    assert run["elapsed_seconds"] == pytest.approx(15, abs=.5)
    assert run["stages"]["upload_receive"]["seconds"] == 12
    assert run["stages"]["upload_save"]["seconds"] == 3
    assert run["resources"] == []


def test_cgroup_v2_uses_effective_parent_limits_and_does_not_export_paths(tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    group = root / "tenant" / "private-name"
    group.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("cpu memory")
    (root / "cpu.max").write_text("max 100000")
    (root / "memory.max").write_text("max")
    (group.parent / "cpu.max").write_text("200000 100000")
    (group.parent / "cpu.stat").write_text("throttled_usec 7500000\nnr_throttled 9")
    (group.parent / "memory.max").write_text("2147483648")
    (group.parent / "memory.current").write_text("765432")
    (group / "cpu.max").write_text("400000 100000")
    (group / "cpu.stat").write_text("throttled_usec 1000000\nnr_throttled 2")
    (group / "memory.max").write_text("4294967296")
    (group / "memory.current").write_text("123456")
    membership = tmp_path / "membership"
    membership.write_text("0::/tenant/private-name")
    monkeypatch.setattr(diag, "CGROUP_ROOT", root)
    monkeypatch.setattr(diag, "PROC_CGROUP", membership)
    result = diag._cgroup()
    assert result == {"cgroup_version": 2, "cgroup_cpu_quota_cores": 2,
                      "cgroup_memory_limit_bytes": 2147483648, "cgroup_memory_current_bytes": 765432,
                      "cgroup_cpu_throttled_seconds": 7.5, "cgroup_cpu_throttled_periods": 9}
    assert "private-name" not in json.dumps(result)


def test_cgroup_v1_and_absent_metrics(tmp_path, monkeypatch):
    root = tmp_path / "cgroup"
    cpu = root / "cpu"
    memory = root / "memory"
    cpu.mkdir(parents=True)
    memory.mkdir()
    (cpu / "cpu.cfs_quota_us").write_text("150000")
    (cpu / "cpu.cfs_period_us").write_text("100000")
    (cpu / "cpu.stat").write_text("throttled_time 250000000\nnr_throttled 2")
    (memory / "memory.limit_in_bytes").write_text("9223372036854771712")  # unlimited v1 sentinel
    (memory / "memory.usage_in_bytes").write_text("524288")
    monkeypatch.setattr(diag, "CGROUP_ROOT", root)
    monkeypatch.setattr(diag, "PROC_CGROUP", tmp_path / "missing")
    values = diag._cgroup()
    assert values["cgroup_cpu_quota_cores"] == 1.5
    assert values["cgroup_memory_limit_bytes"] is None
    assert values["cgroup_cpu_throttled_seconds"] == .25
    leaf = memory / "worker"
    leaf.mkdir()
    (leaf / "memory.limit_in_bytes").write_text("2147483648")
    (leaf / "memory.usage_in_bytes").write_text("123456")
    (memory / "memory.limit_in_bytes").write_text("1073741824")
    membership = tmp_path / "membership"
    membership.write_text("2:memory:/worker")
    monkeypatch.setattr(diag, "PROC_CGROUP", membership)
    values = diag._cgroup()
    assert values["cgroup_memory_limit_bytes"] == 1073741824
    assert values["cgroup_memory_current_bytes"] == 524288
    monkeypatch.setattr(diag, "CGROUP_ROOT", tmp_path / "absent")
    assert diag._cgroup() == {"cgroup_version": 0}
