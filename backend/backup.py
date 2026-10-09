from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SNAPSHOT_DIRS = ("uploads", "audio", "exports")
REQUIRED_TABLES = ("meetings", "users", "sessions")


class BackupError(RuntimeError):
    pass


def _lock_dir(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _lock_tree(root: Path) -> None:
    _lock_dir(root)
    for child in root.rglob("*"):
        if child.is_symlink():
            continue
        try:
            os.chmod(child, 0o700 if child.is_dir() else 0o600)
        except OSError:
            continue


def _outside_data(dest: Path) -> Path:
    target = dest.expanduser().resolve()
    data = config.DATA_DIR.resolve()
    if target == data or data in target.parents:
        raise BackupError("Снимок нельзя класть внутрь data/: следующая копия захватит сама себя")
    return target


def _backup_db(dest: Path) -> None:
    source_path = config.DB_PATH
    if not source_path.exists():
        raise BackupError("Базы ещё нет")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    try:
        source.execute("PRAGMA wal_checkpoint(PASSIVE)")
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    config.restrict_path(dest, 0o600)


def create_snapshot(dest: Path | None = None) -> Path:
    if dest is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = config.ROOT / "backups" / stamp
    target = _outside_data(dest)
    if target.exists() and any(target.iterdir()):
        raise BackupError("Каталог снимка уже не пустой")
    target.mkdir(parents=True, exist_ok=True)
    _lock_dir(target)
    _backup_db(target / "mom.db")
    copied: list[str] = []
    for name in SNAPSHOT_DIRS:
        source = config.DATA_DIR / name
        if not source.exists():
            continue
        shutil.copytree(source, target / name)
        copied.append(name)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "includes": ["mom.db", *copied],
        "omits": ["whisper", "hf"],
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _lock_tree(target)
    checked = verify_snapshot(target)
    if not checked["ok"]:
        raise BackupError("Снимок записан, но проверка целостности не прошла")
    return target


def verify_snapshot(path: Path) -> dict:
    folder = path.expanduser().resolve()
    database = folder / "mom.db"
    if not database.is_file():
        raise BackupError("В снимке нет mom.db")
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        return {"ok": False, "integrity": str(exc), "meetings": 0, "missing_tables": [], "path": str(folder)}
    try:
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            integrity_text = str(integrity[0] if integrity else "")
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing = [name for name in REQUIRED_TABLES if name not in tables]
            meetings = 0
            if "meetings" in tables:
                meetings = int(conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0])
        except sqlite3.DatabaseError as exc:
            return {"ok": False, "integrity": str(exc), "meetings": 0, "missing_tables": [], "path": str(folder)}
    finally:
        conn.close()
    return {
        "ok": integrity_text == "ok" and not missing,
        "integrity": integrity_text,
        "meetings": meetings,
        "missing_tables": missing,
        "path": str(folder),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Снимок записей и базы MoM")
    parser.add_argument("dest", nargs="?", help="Каталог снимка. По умолчанию backups/<дата>")
    parser.add_argument("--check", metavar="PATH", help="Проверить готовый снимок, не создавая новый")
    args = parser.parse_args(argv)
    try:
        if args.check:
            result = verify_snapshot(Path(args.check))
        else:
            folder = create_snapshot(Path(args.dest) if args.dest else None)
            result = verify_snapshot(folder)
    except BackupError as exc:
        print(exc)
        return 1
    state = "ок" if result["ok"] else "повреждён"
    print(f"{result['path']}: {state}, встреч {result['meetings']}")
    if result["missing_tables"]:
        print("нет таблиц: " + ", ".join(result["missing_tables"]))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
