import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile


MAX_FILES = 10000
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_TOTAL_SIZE = 2 * 1024 * 1024 * 1024


def _safe_name(name):
    value = name.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    return ".." not in value.split("/")


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def organize_zip(path, output_dir):
    """Safely extract historical Excel valuation sheets with content dedupe."""
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    known = set()
    for base, _, files in os.walk(output_dir):
        for filename in files:
            if filename.lower().endswith((".xls", ".xlsx")):
                known.add(_hash_file(os.path.join(base, filename)))
    saved, duplicates, ignored = [], 0, []
    with zipfile.ZipFile(path) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) > MAX_FILES:
            raise ValueError("压缩包文件数超过10000")
        if sum(item.file_size for item in entries) > MAX_TOTAL_SIZE:
            raise ValueError("压缩包展开总大小超过2GB")
        for item in entries:
            if not _safe_name(item.filename):
                raise ValueError("压缩包包含不安全路径")
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("压缩包包含符号链接")
            if item.file_size > MAX_FILE_SIZE:
                raise ValueError("压缩包单文件超过100MB")
            if not item.filename.lower().endswith((".xls", ".xlsx")):
                ignored.append(item.filename)
                continue
            parts = [part for part in item.filename.replace("\\", "/").split("/") if part]
            group = parts[-2] if len(parts) > 1 else "待核查"
            group = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", group).strip("._") or "待核查"
            target_dir = os.path.join(output_dir, group)
            os.makedirs(target_dir, exist_ok=True)
            with archive.open(item) as source, tempfile.NamedTemporaryFile(
                    "wb", dir=target_dir, prefix=".extract-", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                digest = hashlib.sha256()
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
            value = digest.hexdigest()
            if value in known:
                os.remove(temporary)
                duplicates += 1
                continue
            filename = os.path.basename(parts[-1])
            target = os.path.join(target_dir, filename)
            if os.path.exists(target):
                stem, extension = os.path.splitext(target)
                target = stem + "_" + value[:8] + extension
            os.replace(temporary, target)
            known.add(value)
            saved.append(target)
    return {"archive": os.path.abspath(path), "saved": len(saved),
            "duplicates": duplicates, "ignored": ignored, "files": saved}


def organize_zip_7zip(path, output_dir, executable=r"C:\Program Files\7-Zip\7z.exe"):
    """Use 7-Zip for legacy Chinese filename decoding, then hash-dedupe files."""
    if not os.path.isfile(executable):
        raise RuntimeError("未找到7-Zip")
    # zipfile remains the security gate; 7-Zip is used only after all members pass.
    with zipfile.ZipFile(path) as archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) > MAX_FILES or sum(item.file_size for item in entries) > MAX_TOTAL_SIZE:
            raise ValueError("压缩包规模超过安全限制")
        for item in entries:
            if (not _safe_name(item.filename) or item.file_size > MAX_FILE_SIZE
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError("压缩包包含不安全项目")
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    known = set()
    for base, _, files in os.walk(output_dir):
        for filename in files:
            if filename.lower().endswith((".xls", ".xlsx")):
                known.add(_hash_file(os.path.join(base, filename)))
    saved, duplicates, ignored = [], 0, []
    work_root = os.path.join(os.path.dirname(output_dir), ".extracting")
    os.makedirs(work_root, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="underlying-history-", dir=work_root) as temporary:
        process = subprocess.run([executable, "x", path, "-o%s" % temporary, "-y", "-bd"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if process.returncode:
            detail = process.stderr.decode("utf-8", "replace")[-300:]
            raise RuntimeError("7-Zip解压失败，退出码%d：%s" % (process.returncode, detail))
        for base, dirs, files in os.walk(temporary):
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
            for filename in files:
                source = os.path.join(base, filename)
                if not filename.lower().endswith((".xls", ".xlsx")):
                    ignored.append(os.path.relpath(source, temporary))
                    continue
                digest = _hash_file(source)
                if digest in known:
                    duplicates += 1
                    continue
                relative = os.path.relpath(base, temporary).split(os.sep)
                group = relative[-1] if relative and relative[-1] != "." else "待核查"
                target_dir = os.path.join(output_dir, group)
                os.makedirs(target_dir, exist_ok=True)
                target = os.path.join(target_dir, filename)
                if os.path.exists(target):
                    stem, extension = os.path.splitext(target)
                    target = stem + "_" + digest[:8] + extension
                shutil.copyfile(source, target + ".tmp")
                os.replace(target + ".tmp", target)
                known.add(digest)
                saved.append(target)
    return {"archive": os.path.abspath(path), "saved": len(saved),
            "duplicates": duplicates, "ignored": ignored, "files": saved}
