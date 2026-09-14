"""Content-addressed, local backup snapshots for irreplaceable project data."""

from __future__ import print_function

import datetime
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile


SCHEMA_VERSION = 1
DEFAULT_RETENTION = 30
SOURCE_DIRECTORIES = ("products", "底层资产", "data_sources", "knowledge_base")
OPTIONAL_DIRECTORIES = (os.path.join("strategy_lab_data", "llm_runs"),)
SOURCE_FILES = ("专户资金台账.xlsx", "产品标签.xlsx", "管理人清单.xlsx", "碳排放价格.xlsx")
AUDIT_FILES = (os.path.join("logs", "label-audit.jsonl"),)
SNAPSHOT_RE = re.compile(r"^snapshot-\d{8}T\d{6}(?:-\d+)?\.json$")


class BackupError(ValueError):
    pass


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".snapshot-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _safe_relative(path):
    value = str(path).replace("\\", "/")
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise BackupError("备份清单包含绝对路径")
    normalized = os.path.normpath(value)
    if normalized == ".." or normalized.startswith(".." + os.sep):
        raise BackupError("备份清单包含路径穿越")
    return normalized


def _iter_sources(project_root):
    seen = set()
    roots = list(SOURCE_DIRECTORIES) + list(OPTIONAL_DIRECTORIES)
    for relative_root in roots:
        absolute_root = os.path.join(project_root, relative_root)
        if not os.path.isdir(absolute_root):
            continue
        for parent, directories, files in os.walk(absolute_root):
            directories[:] = sorted(d for d in directories if d not in ("__pycache__",))
            for filename in sorted(files):
                if filename.endswith((".pyc", ".tmp", "-wal", "-shm")):
                    continue
                absolute = os.path.join(parent, filename)
                relative = os.path.relpath(absolute, project_root)
                if relative not in seen:
                    seen.add(relative)
                    yield relative, absolute
    for relative in SOURCE_FILES + AUDIT_FILES:
        absolute = os.path.join(project_root, relative)
        if os.path.isfile(absolute) and relative not in seen:
            seen.add(relative)
            yield relative, absolute


def _sqlite_copy(source, destination):
    source_connection = sqlite3.connect("file:%s?mode=ro" % source.replace("\\", "/"), uri=True)
    try:
        target_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(target_connection)
            result = target_connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise BackupError("SQLite备份完整性检查失败")
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def _snapshot_name(snapshot_dir):
    base = "snapshot-%s" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    name = base + ".json"
    counter = 1
    while os.path.exists(os.path.join(snapshot_dir, name)):
        name = "%s-%d.json" % (base, counter)
        counter += 1
    return name


def _validate_roots(project_root, backup_root):
    project_root = os.path.realpath(project_root)
    backup_root = os.path.realpath(backup_root)
    try:
        common = os.path.commonpath((project_root, backup_root))
    except ValueError:
        common = ""
    if common == project_root:
        raise BackupError("备份目录不能位于项目目录内")
    return project_root, backup_root


def create_backup(project_root, backup_root, retention=DEFAULT_RETENTION):
    project_root, backup_root = _validate_roots(project_root, backup_root)
    retention = int(retention)
    if retention < 1:
        raise BackupError("备份保留数量必须大于0")
    blob_root = os.path.join(backup_root, "blobs")
    snapshot_root = os.path.join(backup_root, "snapshots")
    os.makedirs(blob_root, exist_ok=True)
    os.makedirs(snapshot_root, exist_ok=True)
    entries = []
    created_blobs = 0
    reused_blobs = 0
    temp_root = tempfile.mkdtemp(prefix="fof-backup-")
    try:
        for relative, source in _iter_sources(project_root):
            backup_source = source
            if source.lower().endswith((".sqlite", ".sqlite3", ".db")):
                backup_source = os.path.join(temp_root, hashlib.sha256(relative.encode("utf-8")).hexdigest())
                _sqlite_copy(source, backup_source)
            digest = _sha256(backup_source)
            blob_path = os.path.join(blob_root, digest[:2], digest)
            if os.path.exists(blob_path):
                reused_blobs += 1
            else:
                os.makedirs(os.path.dirname(blob_path), exist_ok=True)
                temporary = blob_path + ".tmp-%d" % os.getpid()
                shutil.copyfile(backup_source, temporary)
                if _sha256(temporary) != digest:
                    os.remove(temporary)
                    raise BackupError("备份内容写入校验失败：%s" % relative)
                os.replace(temporary, blob_path)
                created_blobs += 1
            stat = os.stat(source)
            entries.append({"path": relative.replace("\\", "/"), "sha256": digest,
                            "size": os.path.getsize(backup_source),
                            "source_mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))})
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)
    snapshot_name = _snapshot_name(snapshot_root)
    manifest = {"schema_version": SCHEMA_VERSION,
                "snapshot_id": snapshot_name[:-5],
                "created_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
                "files": entries}
    _atomic_json(os.path.join(snapshot_root, snapshot_name), manifest)
    retention_result = prune_backups(backup_root, retention)
    return {"status": "ok", "snapshot_id": manifest["snapshot_id"],
            "files": len(entries), "created_blobs": created_blobs,
            "reused_blobs": reused_blobs, "retention": retention_result}


def _snapshot_path(backup_root, snapshot_id=None):
    snapshot_root = os.path.join(os.path.realpath(backup_root), "snapshots")
    names = _snapshot_names(snapshot_root)
    if not names:
        raise BackupError("没有可用的备份快照")
    if snapshot_id:
        name = snapshot_id if snapshot_id.endswith(".json") else snapshot_id + ".json"
        if not SNAPSHOT_RE.match(name) or name not in names:
            raise BackupError("备份快照不存在")
    else:
        name = names[-1]
    return os.path.join(snapshot_root, name)


def _snapshot_names(snapshot_root):
    if not os.path.isdir(snapshot_root):
        return []
    names = [name for name in os.listdir(snapshot_root) if SNAPSHOT_RE.match(name)]
    return sorted(names, key=lambda name: (os.stat(os.path.join(snapshot_root, name)).st_mtime_ns,
                                           name))


def check_backup(backup_root, snapshot_id=None):
    manifest_path = _snapshot_path(backup_root, snapshot_id)
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    errors = []
    checked = 0
    for entry in manifest.get("files", []):
        try:
            _safe_relative(entry["path"])
            digest = entry["sha256"]
            blob_path = os.path.join(os.path.realpath(backup_root), "blobs", digest[:2], digest)
            if not os.path.isfile(blob_path):
                errors.append({"path": entry.get("path"), "error": "blob_missing"})
            elif os.path.getsize(blob_path) != entry.get("size") or _sha256(blob_path) != digest:
                errors.append({"path": entry.get("path"), "error": "hash_mismatch"})
            checked += 1
        except (KeyError, OSError, BackupError) as exc:
            errors.append({"path": entry.get("path"), "error": type(exc).__name__})
    return {"status": "ok" if not errors else "failed",
            "snapshot_id": manifest.get("snapshot_id"), "checked": checked, "errors": errors}


def restore_backup(backup_root, target, snapshot_id=None):
    target = os.path.realpath(target)
    if os.path.exists(target) and os.listdir(target):
        raise BackupError("恢复目标必须是不存在或为空的目录")
    result = check_backup(backup_root, snapshot_id)
    if result["status"] != "ok":
        raise BackupError("备份校验失败，拒绝恢复")
    manifest_path = _snapshot_path(backup_root, snapshot_id)
    with open(manifest_path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    os.makedirs(target, exist_ok=True)
    for entry in manifest["files"]:
        relative = _safe_relative(entry["path"])
        destination = os.path.realpath(os.path.join(target, relative))
        if os.path.commonpath((target, destination)) != target:
            raise BackupError("恢复路径越界")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        digest = entry["sha256"]
        shutil.copyfile(os.path.join(os.path.realpath(backup_root), "blobs", digest[:2], digest), destination)
    return {"status": "ok", "snapshot_id": manifest["snapshot_id"],
            "target": target, "restored": len(manifest["files"])}


def prune_backups(backup_root, retention=DEFAULT_RETENTION):
    snapshot_root = os.path.join(os.path.realpath(backup_root), "snapshots")
    names = _snapshot_names(snapshot_root)
    removed_snapshots = []
    for name in names[:-int(retention)]:
        os.remove(os.path.join(snapshot_root, name))
        removed_snapshots.append(name[:-5])
    referenced = set()
    for name in names[-int(retention):]:
        path = os.path.join(snapshot_root, name)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            referenced.update(item.get("sha256") for item in json.load(handle).get("files", []))
    removed_blobs = 0
    blob_root = os.path.join(os.path.realpath(backup_root), "blobs")
    if os.path.isdir(blob_root):
        for parent, _, files in os.walk(blob_root):
            for name in files:
                if name not in referenced and not ".tmp-" in name:
                    os.remove(os.path.join(parent, name))
                    removed_blobs += 1
    return {"removed_snapshots": removed_snapshots, "removed_blobs": removed_blobs}
