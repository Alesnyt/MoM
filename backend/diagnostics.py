"""Bounded, local performance traces with a deliberately text-free export.

This database is independent of the application database. Diagnostics must never
make processing fail, and collection/export accepts only known numeric fields and
enums. No prompt, response, path, endpoint or exception text is collected.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import re
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterator

import psutil

from . import config, monitoring

log = logging.getLogger("mom.diagnostics")
RETENTION_DAYS = 14
MAX_RUNS = 1000
MAX_EXPORT_RUNS = 200
MAX_EVENTS = 128
MAX_LLM_CALLS = 256
MAX_SDK_ATTEMPTS = 8
MAX_SAMPLES = 360
MAX_STORAGE_BYTES = 64 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
FLUSH_SECONDS = 15.0
SQLITE_TIMEOUT = 0.15
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")
SMAPS_ROLLUP = Path("/proc/self/smaps_rollup")
MEMORY_POINTS = ("before_models", "before_gigaam", "after_gigaam", "before_whisper", "after_whisper",
                 "before_diarization", "after_diarization",
                 "before_idle_unload", "after_idle_unload")
MEMORY_FIELDS = frozenset({"rss_bytes", "pss_anon_bytes", "pss_file_bytes", "pss_shmem_bytes", "offset_seconds", "torch_cpu_threads"})
EMPTY_GIGAAM_STATS = {"count": 0, "under_one_second": 0, "source_seconds": 0., "model_input_seconds": 0.}

STAGES = frozenset({
    "queue_wait", "upload_receive", "upload_save", "context_load", "context_apply",
    "audio_prepare", "local_slot_wait", "asr", "asr_cloud", "diarization",
    "name_review", "protocol", "result_save", "whisper_model_load", "gigaam_model_load",
    "diarization_model_load", "diarization_audio_load", "diarization_inference",
})
PHASES = frozenset({"audio", "protocol", "upload"})
STATUSES = frozenset({"running", "review", "done", "error", "cancelled", "interrupted", "uploaded", "unknown"})
LLM_PHASES = frozenset({"protocol", "protocol_part", "protocol_merge", "name_review"})
LLM_NUMBERS = frozenset({
    "seconds", "input_chars", "input_messages", "prompt_tokens", "completion_tokens", "total_tokens",
    "reasoning_tokens", "cached_tokens", "http_status", "sdk_attempts", "http_attempts", "ttft_seconds",
})
LLM_ENUMS = {
    "finish_reason": frozenset({"stop", "length", "tool_calls", "content_filter", "function_call", "other", "unknown"}),
    "outcome": frozenset({"ok", "error", "cancelled"}),
    "retry_reason": frozenset({"none", "json_fallback", "context_split", "error_retry"}),
    "error_kind": frozenset({"none", "timeout", "connection", "rate_limit", "auth", "server", "request", "cancelled", "other"}),
}
LLM_ENUM_DEFAULTS = {"finish_reason": "unknown", "outcome": "error", "retry_reason": "none", "error_kind": "other"}
RESOURCE_NUMBERS = frozenset({
    "offset_seconds", "process_cpu_percent", "process_rss_bytes", "process_count",
    "host_available_memory_bytes", "host_swap_used_bytes", "host_swap_in_bytes", "host_swap_out_bytes",
    "host_cpu_steal_percent", "host_cpu_iowait_percent", "host_cpu_busy_percent", "cgroup_memory_current_bytes",
    "cgroup_cpu_throttled_seconds", "cgroup_cpu_throttled_periods", "torch_cpu_threads",
})
ENV_NUMBERS = frozenset({
    "logical_cpus", "affinity_cpus", "host_memory_total_bytes", "cgroup_cpu_quota_cores",
    "cgroup_memory_limit_bytes", "cgroup_version",
})
SETTING_NUMBERS = frozenset({
    "max_jobs", "torch_cpu_threads_configured", "whisper_cpu_threads_configured",
    "asr_idle_unload_seconds", "diarize_preload_max_bytes", "diarize_max_speakers", "protocol_context_tokens",
})
_current: ContextVar[Run | None] = ContextVar("mom_diagnostic_run", default=None)
_active: dict[str, Run] = {}
_idle_run: Run | None = None
_active_lock = threading.RLock()
_storage_lock = threading.RLock()


def _number(value: Any) -> int | float | None:
    if type(value) not in (int, float) or value < 0:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return min(value, 10**18)


def _numbers(value: Any, keys: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: _number(value[key]) for key in keys if key in value}


def _enum(value: Any, choices: frozenset[str], fallback: str = "unknown") -> str:
    return value if isinstance(value, str) and value in choices else fallback


def _utc(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds")


def _path() -> Path:
    return config.DATA_DIR / "performance.sqlite3"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_path(), timeout=SQLITE_TIMEOUT)
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, meeting_key TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS runs_started ON runs(started)")
        yield conn
        conn.commit()
    finally:
        conn.close()
    config.restrict_path(_path(), 0o600)


def _prune(conn: sqlite3.Connection, now: float) -> None:
    conn.execute("DELETE FROM runs WHERE started < ?", (now - RETENTION_DAYS * 86400,))
    conn.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY started DESC LIMIT ?)", (MAX_RUNS,))
    # A second bound prevents an unusual stream of long meetings growing the DB.
    total = 0
    remove = []
    for run_id, size in conn.execute("SELECT id, length(payload) FROM runs ORDER BY started DESC"):
        total += size
        if total > MAX_STORAGE_BYTES:
            remove.append((run_id,))
    conn.executemany("DELETE FROM runs WHERE id = ?", remove)


def initialize() -> bool:
    """Called once at application startup. Preserve the last flushed crash trace."""
    try:
        with _storage_lock, _connect() as conn:
            # Read the active registry too: repeated startup in an in-process test
            # must not label a live run as interrupted.
            with _active_lock:
                live = set(_active)
            for run_id, raw in conn.execute("SELECT id, payload FROM runs").fetchall():
                data = _decode(raw)
                if data and data.get("status") == "running" and run_id not in live:
                    data["status"] = "interrupted"
                    data["elapsed_is_partial"] = True
                    conn.execute("UPDATE runs SET payload = ? WHERE id = ?", (_json(data), run_id))
            _prune(conn, time.time())
        return True
    except Exception:
        # Intentionally no exception details: paths may contain personal data.
        log.warning("Performance diagnostics storage unavailable")
        return False


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii")[:4096].strip()
    except (OSError, UnicodeError):
        return ""


def _membership() -> dict[str, str]:
    result = {}
    for line in _read(PROC_CGROUP).splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3:
            for controller in parts[1].split(","):
                result[controller] = parts[2]
    return result


def _group(root: Path, relative: str) -> Path:
    # Container namespaces often expose their own group directly at the root.
    candidate = root / relative.lstrip("/")
    try:
        if candidate.resolve().is_relative_to(root.resolve()) and candidate.is_dir():
            return candidate
    except OSError:
        pass
    return root


def _ancestors(path: Path, root: Path) -> list[Path]:
    return [path, *[parent for parent in path.parents if parent == root or root in parent.parents]]


def _positive(value: str) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 < number < 2**60 else None
    except (ValueError, TypeError):
        return None


def _stat(path: Path) -> dict[str, float]:
    values = {}
    for line in _read(path).splitlines():
        pair = line.split()
        if len(pair) == 2:
            try:
                value = float(pair[1])
                if math.isfinite(value) and value >= 0:
                    values[pair[0]] = value
            except ValueError:
                pass
    return values


def _cgroup() -> dict:
    """Best effort Linux cgroup v2/v1 limits, without exporting mount paths."""
    root = CGROUP_ROOT
    membership = _membership()
    quota, memory_limit = None, None
    if (root / "cgroup.controllers").exists():
        group = _group(root, membership.get("", ""))
        cpu_group = group
        memory_group = group
        for parent in _ancestors(group, root):
            raw = _read(parent / "cpu.max").split()
            if len(raw) == 2:
                amount, period = _positive(raw[0]), _positive(raw[1])
                if amount and period and (quota is None or amount / period < quota):
                    quota, cpu_group = amount / period, parent
            limit = _positive(_read(parent / "memory.max"))
            if limit is not None and (memory_limit is None or limit < memory_limit):
                memory_limit, memory_group = limit, parent
        stats = _stat(cpu_group / "cpu.stat")
        return {"cgroup_version": 2, "cgroup_cpu_quota_cores": quota,
                "cgroup_memory_limit_bytes": memory_limit,
                "cgroup_memory_current_bytes": _positive(_read(memory_group / "memory.current")),
                "cgroup_cpu_throttled_seconds": stats.get("throttled_usec", 0) / 1_000_000 if stats else None,
                "cgroup_cpu_throttled_periods": stats.get("nr_throttled")}
    cpu_root = next((path for path in (root / "cpu", root / "cpu,cpuacct") if path.is_dir()), None)
    memory_root = root / "memory"
    if cpu_root is None and not memory_root.is_dir():
        return {"cgroup_version": 0}
    stats = {}
    if cpu_root:
        group = _group(cpu_root, membership.get("cpu", ""))
        cpu_group = group
        for parent in _ancestors(group, cpu_root):
            amount = _positive(_read(parent / "cpu.cfs_quota_us"))
            period = _positive(_read(parent / "cpu.cfs_period_us"))
            if amount and period and (quota is None or amount / period < quota):
                quota, cpu_group = amount / period, parent
        stats = _stat(cpu_group / "cpu.stat")
    mem_group = _group(memory_root, membership.get("memory", ""))
    memory_group = mem_group
    for parent in _ancestors(mem_group, memory_root):
        limit = _positive(_read(parent / "memory.limit_in_bytes"))
        if limit is not None and (memory_limit is None or limit < memory_limit):
            memory_limit, memory_group = limit, parent
    return {"cgroup_version": 1, "cgroup_cpu_quota_cores": quota,
            "cgroup_memory_limit_bytes": memory_limit,
            "cgroup_memory_current_bytes": _positive(_read(memory_group / "memory.usage_in_bytes")),
            "cgroup_cpu_throttled_seconds": stats.get("throttled_time", 0) / 1_000_000_000 if stats else None,
            "cgroup_cpu_throttled_periods": stats.get("nr_throttled")}


def _affinity() -> int | None:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count()


def _app_version() -> str:
    try:
        value = json.loads((config.ROOT / "frontend/package.json").read_text())["version"]
        match = re.match(r"^(\d+\.\d+\.\d+)(?:-|$)", value) if isinstance(value, str) else None
        return match[1] if match else "unknown"
    except Exception:
        return "unknown"


def _environment() -> dict:
    result = {"logical_cpus": os.cpu_count(), "affinity_cpus": _affinity(),
              "os": _enum(platform.system().lower(), frozenset({"linux", "darwin", "windows"}), "other"),
              "architecture": _enum(platform.machine().lower(), frozenset({"x86_64", "amd64", "arm64", "aarch64"}), "other"),
              "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"}
    try:
        result["host_memory_total_bytes"] = psutil.virtual_memory().total
    except Exception:
        pass
    try:
        result.update(_numbers(_cgroup(), ENV_NUMBERS))
    except Exception:
        pass
    return result


def _model_name(name: str) -> tuple[str, str]:
    folded = name.lower().strip()
    families = ("minimax", "qwen", "gpt", "gemini", "llama", "deepseek", "mistral")
    family = next((item for item in families if item in folded), "custom")
    return family, hashlib.sha256(name.encode()).hexdigest()[:16]


def _settings() -> dict:
    model = config.get_chat_model()
    family, ref = _model_name(model)
    asr = config.get_asr_model()
    engine = "gigaam" if config.is_gigaam_asr(asr) else "whisper" if config.is_local_asr(asr) else "api"
    if asr == config.REMOTE_ASR:
        asr = config.get_asr_connection().model
    result = {
        "llm_family": family, "llm_model_ref": ref,
        "asr_engine": engine, "asr_model_ref": hashlib.sha256(asr.encode()).hexdigest()[:16],
        "max_jobs": config.get_max_jobs(), "torch_cpu_threads_configured": config.TORCH_CPU_THREADS,
        "whisper_cpu_threads_configured": config.WHISPER_CPU_THREADS,
        "asr_idle_unload_seconds": config.ASR_IDLE_UNLOAD_SECONDS,
        "diarize_preload_max_bytes": config.DIARIZE_PRELOAD_MAX_BYTES,
        "diarize_max_speakers": config.DIARIZE_MAX_SPEAKERS,
        "diarize_cache_model": bool(config.DIARIZE_CACHE_MODEL),
        "protocol_context_tokens": config.PROTOCOL_CONTEXT_TOKENS,
        "exact_tokenizer": bool(config.PROTOCOL_TOKENIZER_PATH),
    }
    return result


@dataclass
class Run:
    id: str
    meeting_key: str
    phase: str
    started: float
    clock: float
    path: Path
    data: dict
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    last_flush: float = 0
    previous: tuple | None = None
    sample_stride: int = 1
    finished: bool = False


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _decode(raw: Any) -> dict | None:
    if not isinstance(raw, str) or len(raw) > MAX_PAYLOAD_BYTES:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError, RecursionError):
        return None


def _bounded_payload(data: dict) -> str:
    """Preserve completion and totals when combined detail exceeds the byte cap.

    A shallow snapshot is sufficient while the caller holds the run lock; only
    copied arrays and counters are modified. Active exports use this same bound.
    """
    snapshot = {**data, "dropped": dict(data["dropped"])}
    for key in ("resources", "events", "llm"):
        snapshot[key] = list(data[key])
    payload = _json(snapshot)
    while len(payload) > MAX_PAYLOAD_BYTES:
        key = max(("resources", "events", "llm"), key=lambda name: len(snapshot[name]))
        values = snapshot[key]
        if not values:
            break
        thinned = values[::2] if len(values) > 1 else []
        snapshot["dropped"][key] += len(values) - len(thinned)
        snapshot[key] = thinned
        payload = _json(snapshot)
    return payload


def _persist(run: Run, *, prune: bool = False, existing_only: bool = False) -> None:
    try:
        with _storage_lock, _connect() as conn:
            # Snapshot after acquiring the writer lock: a waiting sampler must
            # never overwrite a concurrently completed run with stale status.
            with run.lock:
                payload = _bounded_payload(run.data)
            if len(payload) > MAX_PAYLOAD_BYTES or run.path != _path():
                return
            if existing_only:
                conn.execute("UPDATE runs SET updated = ?, payload = ? WHERE id = ?", (time.time(), payload, run.id))
            else:
                conn.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?)",
                             (run.id, run.meeting_key, run.started, time.time(), payload))
            if prune:
                _prune(conn, time.time())
    except Exception:
        log.warning("Performance diagnostics write skipped")


def begin_run(meeting_id: str, phase: str, *, queue_wait_seconds: float | None = None,
              audio_seconds: float | None = None) -> Run:
    now = time.time()
    clock = time.monotonic()
    data = {"schema_version": 1, "phase": _enum(phase, PHASES, "audio"), "status": "running",
            "app_version": _app_version(), "elapsed_seconds": 0, "elapsed_is_partial": True,
            "audio_seconds": _number(audio_seconds), "queue_wait_seconds": _number(queue_wait_seconds),
            "size_bytes": None, "settings": {}, "environment": {}, "stages": {},
            "events": [], "llm": [], "resources": [], "resource_sample_count": 0,
            "peak_process_rss_bytes": None, "peak_process_cpu_percent": None,
            "min_host_available_memory_bytes": None,
            "dropped": {"events": 0, "llm": 0, "resources": 0}}
    try:
        data["settings"] = _settings()
        data["environment"] = _environment()
    except Exception:
        pass
    run = Run(uuid.uuid4().hex, hashlib.sha256(str(meeting_id).encode()).hexdigest(),
              data["phase"], now, clock, _path(), data, last_flush=clock)
    if data["queue_wait_seconds"] is not None:
        data["stages"]["queue_wait"] = {"count": 1, "seconds": data["queue_wait_seconds"], "interrupted": 0}
    with _active_lock:
        _active[run.id] = run
    _persist(run, prune=True)
    return run


@contextmanager
def run_scope(run: Run) -> Iterator[Run]:
    token = _current.set(run)
    try:
        yield run
    finally:
        _current.reset(token)


def _memory_point(run: Run) -> dict:
    # smaps_rollup walks Linux page tables. Read only at model boundaries.
    point = {"offset_seconds": round(max(0, time.monotonic() - run.clock), 6)}
    fields = {"Rss": "rss_bytes", "Pss_Anon": "pss_anon_bytes",
              "Pss_File": "pss_file_bytes", "Pss_Shmem": "pss_shmem_bytes"}
    for name, value in re.findall(r"^(\w+):\s+(\d+) kB$", _read(SMAPS_ROLLUP), re.MULTILINE):
        if name in fields:
            point[fields[name]] = int(value) * 1024
    if "rss_bytes" not in point:
        try:
            point["rss_bytes"] = psutil.Process().memory_info().rss
        except (psutil.Error, OSError):
            pass
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            point["torch_cpu_threads"] = torch.get_num_threads()
        except Exception:
            pass
    return point


def record_model_memory(point: str) -> None:
    global _idle_run
    run = _current.get()
    if run is None or run.phase != "audio" or point not in MEMORY_POINTS:
        return
    try:
        sample = _memory_point(run)
        with run.lock:
            if run.finished:
                return
            points = run.data.setdefault("model_memory", {})
            if point.startswith("before_"):
                points.setdefault("before_models", sample)
            points[point] = sample
            if point == "after_gigaam" and "gigaam_fragments" not in run.data:
                run.data["gigaam_fragments"] = EMPTY_GIGAAM_STATS.copy()
        # Called inside the shared local-model slot. Associate the next idle
        # unload with its most recent user, even if another meeting finishes later.
        with _active_lock:
            _idle_run = run
    except Exception:
        log.debug("Model memory checkpoint unavailable")


@contextmanager
def idle_memory_scope() -> Iterator[None]:
    global _idle_run
    with _active_lock:
        run = _idle_run
    if run is None or not run.finished or run.path != _path():
        yield
        return
    try:
        before = _memory_point(run)
    except Exception:
        before = {}
    yield  # If unloading fails, preserve the association for the next attempt.
    with _active_lock:
        if _idle_run is not run:
            return
        _idle_run = None
    try:
        after = _memory_point(run)
        with run.lock:
            points = run.data["model_memory"]
            points["before_idle_unload"] = before
            points["after_idle_unload"] = after
        _persist(run, prune=True, existing_only=True)
    except Exception:
        log.debug("Idle memory checkpoint unavailable")


def record_gigaam_input(source_samples: int, input_samples: int) -> None:
    run = _current.get()
    if run is None or run.phase != "audio":
        return
    try:
        with run.lock:
            if run.finished:
                return
            if "gigaam_fragments" not in run.data:
                run.data["gigaam_fragments"] = EMPTY_GIGAAM_STATS.copy()
            stats = run.data["gigaam_fragments"]
            stats["count"] += 1
            stats["under_one_second"] += int(source_samples < 16000)
            stats["source_seconds"] += source_samples / 16000
            stats["model_input_seconds"] += input_samples / 16000
    except Exception:
        log.debug("GigaAM fragment observation unavailable")


def record_stage(stage: str, seconds: float, outcome: str = "ok") -> None:
    run = _current.get()
    value = _number(seconds)
    if run is None or stage not in STAGES or value is None:
        return
    try:
        with run.lock:
            if run.finished:
                return
            outcome = _enum(outcome, frozenset({"ok", "interrupted"}), "interrupted")
            totals = run.data["stages"].setdefault(stage, {"count": 0, "seconds": 0, "interrupted": 0})
            totals["count"] += 1
            totals["seconds"] = round(totals["seconds"] + value, 6)
            totals["interrupted"] += int(outcome != "ok")
            if len(run.data["events"]) < MAX_EVENTS:
                run.data["events"].append({"stage": stage, "seconds": round(value, 6), "outcome": outcome,
                                           "offset_seconds": round(max(0, time.monotonic() - run.clock), 6)})
            else:
                run.data["dropped"]["events"] += 1
    except Exception:
        pass


def record_llm(phase: str, metrics: dict) -> None:
    run = _current.get()
    if run is None or phase not in LLM_PHASES:
        return
    try:
        record = _numbers(metrics, LLM_NUMBERS)
        record.update(_safe_attempts(metrics))
        for key, choices in LLM_ENUMS.items():
            if key in metrics:
                record[key] = _enum(metrics[key], choices, LLM_ENUM_DEFAULTS[key])
        record["phase"] = phase
        record["offset_seconds"] = round(max(0, time.monotonic() - run.clock), 6)
        with run.lock:
            if run.finished:
                return
            if len(run.data["llm"]) < MAX_LLM_CALLS:
                run.data["llm"].append(record)
            else:
                run.data["dropped"]["llm"] += 1
    except Exception:
        pass


def _safe_attempts(event: dict) -> dict:
    attempts = event.get("sdk_attempt_details")
    if not isinstance(attempts, list):
        return {}
    result = []
    for item in attempts[:MAX_SDK_ATTEMPTS]:
        if isinstance(item, dict):
            values = _numbers(item, frozenset({"attempt", "seconds", "gap_before_seconds", "http_status"}))
            values["error_kind"] = _enum(item.get("error_kind"), LLM_ENUMS["error_kind"], "other")
            result.append(values)
    return {"sdk_attempt_details": result, "omitted_sdk_attempts": _number(event.get("omitted_sdk_attempts"))}


def _resource_values() -> tuple[dict, float | None, tuple | None]:
    values: dict = {}
    cpu_seconds = None
    host = None
    try:
        cpu_seconds, values["process_rss_bytes"], values["process_count"] = monitoring._process_totals()
    except Exception:
        pass
    try:
        values["host_available_memory_bytes"] = psutil.virtual_memory().available
        swap = psutil.swap_memory()
        values.update(host_swap_used_bytes=swap.used, host_swap_in_bytes=swap.sin, host_swap_out_bytes=swap.sout)
        cpu = psutil.cpu_times()
        names = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
        host = (sum(getattr(cpu, key, 0.0) for key in names), getattr(cpu, "steal", None),
                getattr(cpu, "iowait", None), getattr(cpu, "idle", None))
    except Exception:
        pass
    try:
        values.update(_numbers(_cgroup(), RESOURCE_NUMBERS))
    except Exception:
        pass
    try:
        torch = sys.modules.get("torch")
        if torch is not None:
            values["torch_cpu_threads"] = torch.get_num_threads()
    except Exception:
        pass
    return values, cpu_seconds, host


def sample_run(run: Run) -> None:
    try:
        values, cpu, host = _resource_values()
        now = time.monotonic()
        with run.lock:
            if run.finished:
                return
            values["offset_seconds"] = round(max(0, now - run.clock), 6)
            if run.previous:
                previous_at, previous_cpu, previous_host = run.previous
                elapsed = now - previous_at
                if elapsed > 0 and cpu is not None and previous_cpu is not None and cpu >= previous_cpu:
                    values["process_cpu_percent"] = round(100 * (cpu - previous_cpu) / elapsed, 3)
                if host and previous_host and host[0] > previous_host[0]:
                    for index, key in ((1, "host_cpu_steal_percent"), (2, "host_cpu_iowait_percent")):
                        if host[index] is not None and previous_host[index] is not None and host[index] >= previous_host[index]:
                            values[key] = round(100 * (host[index] - previous_host[index]) / (host[0] - previous_host[0]), 3)
                    if len(host) > 3 and len(previous_host) > 3 and host[3] is not None and previous_host[3] is not None:
                        idle = host[3] - previous_host[3]
                        waiting = ((host[2] or 0) - (previous_host[2] or 0))
                        if idle >= 0 and waiting >= 0:
                            busy = 100 * (1 - (idle + waiting) / (host[0] - previous_host[0]))
                            values["host_cpu_busy_percent"] = round(min(100, max(0, busy)), 3)
            run.previous = (now, cpu, host)
            values = _numbers(values, RESOURCE_NUMBERS)
            run.data["resource_sample_count"] += 1
            for field_name, source in (("peak_process_rss_bytes", "process_rss_bytes"), ("peak_process_cpu_percent", "process_cpu_percent")):
                value = values.get(source)
                if value is not None:
                    run.data[field_name] = max(run.data[field_name] or 0, value)
            available = values.get("host_available_memory_bytes")
            if available is not None:
                previous = run.data["min_host_available_memory_bytes"]
                run.data["min_host_available_memory_bytes"] = min(previous, available) if previous is not None else available
            samples = run.data["resources"]
            if run.data["resource_sample_count"] % run.sample_stride == 0:
                if len(samples) >= MAX_SAMPLES:
                    samples[:] = samples[::2]
                    run.sample_stride *= 2
                samples.append(values)
            run.data["dropped"]["resources"] = run.data["resource_sample_count"] - len(samples)
            run.data["elapsed_seconds"] = values["offset_seconds"]
            flush = now - run.last_flush >= FLUSH_SECONDS
            if flush:
                run.last_flush = now
        if flush:
            _persist(run, prune=True)
    except Exception:
        pass


def finish_run(run: Run, *, status: str, audio_seconds: float | None = None) -> None:
    try:
        with run.lock:
            if run.finished:
                return
            run.finished = True
            run.data["status"] = _enum(status, STATUSES)
            run.data["elapsed_seconds"] = round(max(0, time.monotonic() - run.clock), 6)
            run.data["elapsed_is_partial"] = status == "interrupted"
            if audio_seconds is not None:
                run.data["audio_seconds"] = _number(audio_seconds)
        _persist(run, prune=True)
    except Exception:
        log.warning("Performance diagnostics completion skipped")
    finally:
        with _active_lock:
            _active.pop(run.id, None)


def record_upload(meeting_id: str, *, receive_seconds: float, save_seconds: float, size_bytes: int) -> None:
    receive, save = _number(receive_seconds), _number(save_seconds)
    if receive is None or save is None:
        return
    run = begin_run(meeting_id, "upload")
    elapsed = receive + save
    run.started -= elapsed
    run.clock -= elapsed
    run.data["size_bytes"] = _number(size_bytes)
    with run_scope(run):
        record_stage("upload_receive", receive)
        record_stage("upload_save", save)
    finish_run(run, status="uploaded")


def _safe_settings(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    result = _numbers(raw, SETTING_NUMBERS)
    for key in ("exact_tokenizer", "diarize_cache_model"):
        if type(raw.get(key)) is bool:
            result[key] = raw[key]
    for key in ("llm_model_ref", "asr_model_ref"):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{16}", value):
            result[key] = value
    result["llm_family"] = _enum(raw.get("llm_family"), frozenset({"minimax", "qwen", "gpt", "gemini", "llama", "deepseek", "mistral", "custom"}), "custom")
    result["asr_engine"] = _enum(raw.get("asr_engine"), frozenset({"gigaam", "whisper", "api"}), "api")
    return result


def _safe_environment(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    result = _numbers(raw, ENV_NUMBERS)
    result["os"] = _enum(raw.get("os"), frozenset({"linux", "darwin", "windows"}), "other")
    result["architecture"] = _enum(raw.get("architecture"), frozenset({"x86_64", "amd64", "arm64", "aarch64"}), "other")
    if isinstance(raw.get("python"), str) and re.fullmatch(r"\d+\.\d+\.\d+", raw["python"]):
        result["python"] = raw["python"]
    return result


def _safe_data(raw: dict) -> dict:
    """Apply the allowlist again when reading: even injected DB text cannot leak."""
    result = _numbers(raw, frozenset({"elapsed_seconds", "audio_seconds", "queue_wait_seconds", "size_bytes", "resource_sample_count",
                                    "peak_process_rss_bytes", "peak_process_cpu_percent", "min_host_available_memory_bytes"}))
    result.update(phase=_enum(raw.get("phase"), PHASES, "audio"), status=_enum(raw.get("status"), STATUSES),
                  elapsed_is_partial=raw.get("elapsed_is_partial") is True,
                  settings=_safe_settings(raw.get("settings")), environment=_safe_environment(raw.get("environment")))
    version = raw.get("app_version")
    result["app_version"] = version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown"
    stages = raw.get("stages")
    result["stages"] = {name: _numbers(values, frozenset({"count", "seconds", "interrupted"}))
                        for name, values in stages.items() if name in STAGES} if isinstance(stages, dict) else {}
    memory = raw.get("model_memory")
    if isinstance(memory, dict):
        result["model_memory"] = {point: _numbers(memory[point], MEMORY_FIELDS) for point in MEMORY_POINTS
                                  if isinstance(memory.get(point), dict)}
    if isinstance(raw.get("gigaam_fragments"), dict):
        result["gigaam_fragments"] = _numbers(raw["gigaam_fragments"], frozenset(EMPTY_GIGAAM_STATS))
    result["events"] = []
    for event in (raw.get("events") or [])[:MAX_EVENTS] if isinstance(raw.get("events"), list) else []:
        if isinstance(event, dict) and isinstance(event.get("stage"), str) and event["stage"] in STAGES:
            result["events"].append({**_numbers(event, frozenset({"seconds", "offset_seconds"})), "stage": event["stage"],
                                     "outcome": _enum(event.get("outcome"), frozenset({"ok", "interrupted"}), "interrupted")})
    result["llm"] = []
    for event in (raw.get("llm") or [])[:MAX_LLM_CALLS] if isinstance(raw.get("llm"), list) else []:
        if isinstance(event, dict) and isinstance(event.get("phase"), str) and event["phase"] in LLM_PHASES:
            record = {**_numbers(event, LLM_NUMBERS | {"offset_seconds"}), "phase": event["phase"]}
            record.update(_safe_attempts(event))
            for key, choices in LLM_ENUMS.items():
                if key in event:
                    record[key] = _enum(event[key], choices, LLM_ENUM_DEFAULTS[key])
            result["llm"].append(record)
    result["resources"] = [_numbers(item, RESOURCE_NUMBERS) for item in raw.get("resources", [])[:MAX_SAMPLES]] if isinstance(raw.get("resources"), list) else []
    result["dropped"] = _numbers(raw.get("dropped"), frozenset({"events", "llm", "resources"}))
    audio = result.get("audio_seconds")
    asr = result["stages"].get("asr", {}).get("seconds")
    ratio = _number(asr / audio) if audio and asr is not None else None
    result["asr_rtf"] = round(ratio, 6) if ratio is not None else None
    result["model_loads"] = {name: result["stages"].get(name, {}).get("count", 0)
                             for name in ("gigaam_model_load", "whisper_model_load", "diarization_model_load")}
    return result


def _rows(days: int) -> tuple[list[tuple], bool]:
    try:
        with _storage_lock, _connect() as conn:
            now = time.time()
            _prune(conn, now)
            rows = conn.execute("SELECT id, meeting_key, started, payload FROM runs WHERE started >= ? ORDER BY started DESC LIMIT ?",
                                (now - days * 86400, MAX_RUNS)).fetchall()
        # Active traces can contain samples newer than the last durable flush.
        with _active_lock:
            active = list(_active.values())
        by_id = {row[0]: row for row in rows}
        for run in active:
            if run.path == _path() and run.started >= now - days * 86400:
                with run.lock:
                    by_id[run.id] = (run.id, run.meeting_key, run.started, _bounded_payload(run.data))
        return sorted(by_id.values(), key=lambda row: row[2], reverse=True)[:MAX_RUNS], True
    except Exception:
        return [], False


def _days(days: int) -> int:
    return max(1, min(RETENTION_DAYS, int(days)))


def summary(days: int = 7) -> dict:
    days = _days(days)
    rows, available = _rows(days)
    result = {"available": available, "days": days, "retention_days": RETENTION_DAYS,
              "runs": 0, "completed": 0, "interrupted": 0, "errors": 0, "active": 0, "stages": {}, "recent_runs": []}
    if not available:
        result["error"] = "storage_unavailable"
        return result
    values: dict[str, list[float]] = {}
    meetings = {}
    for _, key, started, payload in rows:
        raw = _decode(payload)
        if raw is None:
            continue
        try:
            run = _safe_data(raw)
            started_at = _utc(started)
        except (ValueError, TypeError, OverflowError):
            continue
        result["runs"] += 1
        status = run["status"]
        result["completed"] += status in {"done", "review", "uploaded"}
        result["interrupted"] += status in {"cancelled", "interrupted"}
        result["errors"] += status == "error"
        result["active"] += status == "running"
        for stage, data in run["stages"].items():
            if data.get("seconds") is not None and not data.get("interrupted"):
                values.setdefault(stage, []).append(data["seconds"])
        if len(result["recent_runs"]) < 20:
            meeting = meetings.setdefault(key, f"meeting-{len(meetings) + 1:03d}")
            calls = run["llm"]
            retries = None
            if calls and not run["dropped"].get("llm") and all(call.get("sdk_attempts") for call in calls):
                retries = sum(call["sdk_attempts"] - 1 for call in calls)
            result["recent_runs"].append({"run": f"run-{result['runs']:03d}", "meeting": meeting,
                "phase": run["phase"], "status": status, "started_at": started_at,
                "elapsed_seconds": run.get("elapsed_seconds"), "audio_seconds": run.get("audio_seconds"),
                "asr_rtf": run.get("asr_rtf"), "llm_calls": len(run["llm"]) + (run["dropped"].get("llm") or 0),
                "peak_rss_bytes": run.get("peak_process_rss_bytes"),
                "after_idle_rss_bytes": run.get("model_memory", {}).get("after_idle_unload", {}).get("rss_bytes"),
                "gigaam_fragments": run.get("gigaam_fragments", {}).get("count"),
                "gigaam_source_seconds": run.get("gigaam_fragments", {}).get("source_seconds"),
                "sdk_retries": retries})
    for stage, samples in values.items():
        samples.sort()
        result["stages"][stage] = {"samples": len(samples), "median_seconds": round(median(samples), 3),
                                  "p95_seconds": round(samples[math.ceil(len(samples) * .95) - 1], 3)}
    return result


def export_report(days: int = 7) -> dict:
    days = _days(days)
    rows, available = _rows(days)
    report = {"schema_version": 1, "available": available, "generated_at": _utc(time.time()),
              "period_days": days, "retention_days": RETENTION_DAYS, "total_selected_runs": len(rows),
              "exported_runs": 0, "truncated": False, "skipped_invalid_runs": 0, "runs": [],
              "limits": {"stored_runs": MAX_RUNS, "exported_runs": MAX_EXPORT_RUNS,
                         "stage_events_per_run": MAX_EVENTS, "llm_calls_per_run": MAX_LLM_CALLS,
                         "sdk_attempt_details_per_call": MAX_SDK_ATTEMPTS,
                         "resource_samples_per_run": MAX_SAMPLES, "export_bytes": MAX_REPORT_BYTES},
              "semantics": {
                  "resources": "process_and_children_shared_by_concurrent_runs; peaks_are_sampled",
                  "host_metrics": "host_or_VM_not_container; cgroup_limits_reported_separately",
                  "host_cpu_busy_percent": "total_minus_idle_and_iowait; includes_steal; 100_percent_is_all_host_cores",
                  "cpu_percent": "100_percent_is_one_logical_core",
                  "cgroup_throttling": "cumulative_counter_seconds_and_periods_at_effective_cpu_quota_group",
                  "cgroup_memory": "usage_at_effective_memory_limit_group; may_include_sibling_groups",
                  "elapsed": "automatic_run_only; excludes_queue_and_manual_speaker_review",
                  "nested_stages": "model_load_and_substages_are_in_parent_stage; do_not_add_twice",
                  "llm_seconds": "logical_completion_call_including_SDK_retries; no_nonstreaming_TTFT",
                  "stage_aggregates": "sum_of_calls_within_run; includes_interrupted_calls",
                  "model_refs": "opaque_hash_of_configured_model; no_paths_or_endpoint_addresses",
                  "asr_rtf": "ASR_seconds_including_model_load_divided_by_full_audio_seconds",
                  "resource_timeline": "decimated_when_bounded; peaks_include_all_samples",
                  "model_memory": "main_process_only; before_first_local_stage_and_after_each_named_stage; idle_points_on_last_local_model_run_after_queue_idle",
                  "gigaam_fragments": "attempted_inferences; source_PCM_and_padded_model_input_seconds; short_means_source_under_one_second",
                  "sdk_attempt_details": "one_SDK_send_including_redirects; gap_includes_backoff_and_client_work; first_attempts_and_last_retained",
              }}
    if not available:
        report["error"] = "storage_unavailable"
        return report
    meetings = {}
    size = len(_json(report))
    for _, key, started, payload in rows:
        raw = _decode(payload)
        if raw is None:
            report["skipped_invalid_runs"] += 1
            continue
        try:
            run = _safe_data(raw)
            run["started_at"] = _utc(started)
        except (ValueError, TypeError, OverflowError):
            report["skipped_invalid_runs"] += 1
            continue
        run["meeting"] = meetings.setdefault(key, f"meeting-{len(meetings) + 1:03d}")
        run["run"] = f"run-{len(report['runs']) + 1:03d}"
        run_size = len(_json(run)) + 1
        if len(report["runs"]) >= MAX_EXPORT_RUNS or size + run_size > MAX_REPORT_BYTES - 1024:
            report["truncated"] = True
            break
        size += run_size
        report["runs"].append(run)
    report["exported_runs"] = len(report["runs"])
    return report
