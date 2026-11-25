from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
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


def _originals(conn: sqlite3.Connection) -> dict[str, str]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(meetings)")}
    source = "json_extract(segments_json, '$.source_type')" if "segments_json" in columns else "NULL"
    rows = conn.execute(f"SELECT id, filename, {source} FROM meetings")
    return {mid: mid + (Path(name).suffix.lower() or ".webm") for mid, name, kind in rows
            if kind != "text" and Path(name).suffix.lower() not in {".txt", ".md", ".srt", ".vtt"}}


def create_snapshot(dest: Path | None = None) -> Path:
    if dest is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = config.ROOT / "backups" / stamp
    target = _outside_data(dest)
    if target.exists() and any(target.iterdir()):
        raise BackupError("Каталог снимка уже не пустой")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=target.parent) as temporary:
        stage = Path(temporary) / "snapshot"
        _backup_db(stage / "mom.db")
        with closing(sqlite3.connect(f"{(stage / 'mom.db').as_uri()}?mode=ro&immutable=1", uri=True)) as conn:
            originals = _originals(conn)
        copied = []
        for name in SNAPSHOT_DIRS:
            source = config.DATA_DIR / name
            if source.exists():
                if source.is_symlink():
                    raise BackupError(f"Символьная ссылка в каталоге данных: {name}")
                if name == "uploads":
                    allowed = set(originals.values())
                    # A database snapshot fixes membership even while uploads continue.
                    shutil.copytree(source, stage / name, symlinks=True,
                                    ignore=lambda folder, entries: set(entries) - allowed)
                elif name == "audio":
                    (stage / name).mkdir()
                    for file in source.iterdir():
                        if file.stem in originals and file.suffix in {".wav", ".mp3"}:
                            try:
                                shutil.copy2(file, stage / name / file.name, follow_symlinks=False)
                            except FileNotFoundError:
                                pass  # Derived audio may finish processing during a snapshot.
                else:
                    shutil.copytree(source, stage / name, symlinks=True)
                copied.append(name)
        manifest = {
            "version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "includes": ["mom.db", *copied],
            "omits": ["whisper", "hf"],
            "sha256": _checksums(stage),
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        _lock_tree(stage)
        checked = _verify_snapshot(stage, manifest["sha256"])
        if not checked["ok"]:
            raise BackupError("Проверка снимка не прошла: " + ", ".join(checked["file_errors"] or [checked["integrity"]]))
        os.rename(stage, target)
    return target


def _checksums(folder: Path) -> dict[str, str]:
    files = [folder / "mom.db"]
    for name in SNAPSHOT_DIRS:
        directory = folder / name
        if directory.is_symlink():
            raise BackupError(f"Символьная ссылка в снимке: {name}")
        files.extend(directory.rglob("*"))
    checksums = {}
    for path in sorted(files):
        relative = path.relative_to(folder).as_posix()
        if path.is_symlink():
            raise BackupError(f"Символьная ссылка в снимке: {relative}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise BackupError(f"Файл снимка недоступен: {relative}")
        with path.open("rb") as stream:
            checksums[relative] = hashlib.file_digest(stream, "sha256").hexdigest()
    return checksums


def verify_snapshot(path: Path) -> dict:
    return _verify_snapshot(path)


def _verify_snapshot(path: Path, calculated: dict[str, str] | None = None) -> dict:
    folder = path.expanduser().resolve()
    database = folder / "mom.db"
    if database.is_symlink() or not database.is_file():
        raise BackupError("В снимке нет mom.db")
    result = {"ok": False, "integrity": "", "meetings": 0, "missing_tables": [],
              "path": str(folder), "file_errors": [], "checksums": False}
    try:
        conn = sqlite3.connect(f"{database.as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.DatabaseError as exc:
        return {**result, "integrity": str(exc)}
    try:
        result["integrity"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        result["missing_tables"] = [name for name in REQUIRED_TABLES if name not in tables]
        originals = {}
        if "meetings" in tables:
            result["meetings"] = conn.execute("SELECT count(*) FROM meetings").fetchone()[0]
            originals = _originals(conn)
    except sqlite3.DatabaseError as exc:
        return {**result, "integrity": str(exc)}
    finally:
        conn.close()
    errors = result["file_errors"]
    try:
        files = calculated if calculated is not None else _checksums(folder)
        uploads = {Path(name).name for name in files if Path(name).parent == Path("uploads")}
        manifest = json.loads((folder / "manifest.json").read_text())
        if manifest.get("version") == 2:
            errors.extend(f"Нет оригинала встречи {mid}" for mid, filename in originals.items() if filename not in uploads)
            errors.extend(f"Файл без встречи {name}" for name in sorted(uploads - set(originals.values())))
        else:
            stems = {Path(name).stem for name in uploads}
            errors.extend(f"Нет оригинала встречи {mid}" for mid in originals if mid not in stems)
        expected = manifest.get("sha256")
        if expected is not None:
            result["checksums"] = True
            errors.extend(f"Изменён или отсутствует файл {name}" for name, digest in expected.items()
                          if files.get(name) != digest)
            errors.extend(f"Лишний файл {name}" for name in files.keys() - expected.keys())
    except (BackupError, OSError, ValueError, AttributeError) as exc:
        errors.append(f"Не удалось проверить файлы снимка: {exc}")
    result["ok"] = result["integrity"] == "ok" and not result["missing_tables"] and not errors
    return result


def _hold_restored_mail(stage: Path) -> bool:
    """SMTP cannot tell whether a pending-at-snapshot message was delivered later."""
    with closing(sqlite3.connect(stage / "mom.db")) as conn, conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "protocol_mail" not in tables:
            return False
        meetings = conn.execute("SELECT DISTINCT meeting_id FROM protocol_mail WHERE state IN ('pending','sending')").fetchall()
        if not meetings:
            return False
        conn.execute("UPDATE protocol_mail SET state='unknown', body='', subject='' WHERE state IN ('pending','sending')")
        conn.executemany("UPDATE meetings SET status_message=? WHERE id=? AND status='done'",
                         [("После восстановления проверьте доставку перед повторной отправкой.", mid) for mid, in meetings])
        if "protocol_mail_format" in tables:
            conn.executemany("DELETE FROM protocol_mail_format WHERE meeting_id=?", meetings)
    manifest_file = stage / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    manifest["mail_held_on_restore"] = len(meetings)
    if "sha256" in manifest:
        with (stage / "mom.db").open("rb") as db:
            manifest["sha256"]["mom.db"] = hashlib.file_digest(db, "sha256").hexdigest()
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return True


def restore_snapshot(source: Path, dest: Path) -> Path:
    source = source.expanduser().resolve()
    target = _outside_data(dest)
    if target == source or source in target.parents or (target.exists() and any(target.iterdir())):
        raise BackupError("Для восстановления нужен отдельный пустой каталог")
    if not verify_snapshot(source)["ok"]:
        raise BackupError("Копия не прошла проверку")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as temporary:
        stage = Path(temporary) / "data"
        stage.mkdir()
        for name in ("mom.db", "manifest.json", *SNAPSHOT_DIRS):
            item = source / name
            if item.is_dir():
                shutil.copytree(item, stage / name, symlinks=True)
            elif item.exists():
                shutil.copy2(item, stage / name)
        if not verify_snapshot(stage)["ok"]:
            raise BackupError("Восстановленные файлы не прошли проверку")
        if _hold_restored_mail(stage) and not verify_snapshot(stage)["ok"]:
            raise BackupError("Восстановленная база не прошла проверку очереди писем")
        _lock_tree(stage)
        os.rename(stage, target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Снимок записей и базы MoM")
    parser.add_argument("dest", nargs="?", help="Каталог снимка. По умолчанию backups/<дата>")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", metavar="PATH", help="Проверить готовый снимок")
    mode.add_argument("--restore", metavar="PATH", help="Восстановить снимок в указанный пустой каталог")
    args = parser.parse_args(argv)
    try:
        if args.check:
            result = verify_snapshot(Path(args.check))
        elif args.restore:
            if not args.dest:
                parser.error("Укажите пустой каталог для восстановления")
            print(restore_snapshot(Path(args.restore), Path(args.dest)))
            return 0
        else:
            folder = create_snapshot(Path(args.dest) if args.dest else None)
            print(f"{folder}: ок")
            return 0
    except (BackupError, OSError) as exc:
        print(exc)
        return 1
    state = "ок" if result["ok"] else "повреждён"
    print(f"{result['path']}: {state}, встреч {result['meetings']}")
    if result["missing_tables"]:
        print("нет таблиц: " + ", ".join(result["missing_tables"]))
    for error in result["file_errors"]:
        print(error)
    if result["ok"] and not result["checksums"]:
        print("Снимок старого формата, контрольные суммы отсутствуют")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
