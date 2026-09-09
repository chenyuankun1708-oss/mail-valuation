import email
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

try:
    import rarfile
except ImportError:  # Python 3.7 deployment can use 7-Zip listing directly.
    rarfile = None

from .mail import _connect, _decode, accounts, load_env


ACCOUNT = "1144123001@qq.com"
SENDER = "jiachen_dwzq@foxmail.com"
ATTACHMENT_NAMES = {"底层资产.rar", "fof.rar"}
ALLOWED_PRODUCTS = {
    "JIAY01": "西南证券嘉盈1号FOF单一资产管理计划",
    "SALT58": "第一创业天玑13号单一资产管理计划",
    "SASF45": "国金资管盛乾同行2号FOF单一资产管理计划",
    "SATQ22": "第一创业尊享FOF55号单一资产管理计划",
    "SAVX98": "招商资管元盈FOF1号单一资产管理计划",
    "TC6WJ4": "第一创业源泉优享FOF3号单一资产管理计划",
}
EXCLUDED_CODES = {"SBPR93", "SBPR95"}
MAX_FILES = 500
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_TOTAL_SIZE = 1024 * 1024 * 1024


def match_archive_attachment(filename):
    """Match only archive names used by the fixed trusted sender."""
    normalized = os.path.basename((filename or "").strip()).lower()
    return normalized in ATTACHMENT_NAMES or bool(re.match(r"^20\d{6}\.rar$", normalized))


def _atomic_json(path, payload):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                         prefix=".manifest-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _manifest(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {"attachments": {}}
    except (OSError, ValueError):
        return {"attachments": {}}


def _extractor(env_path=".env"):
    env = load_env(env_path)
    candidates = [os.environ.get("RAR_EXTRACTOR", ""), env.get("RAR_EXTRACTOR", ""),
                  r"C:\Program Files\7-Zip\7z.exe",
                  r"C:\Program Files (x86)\7-Zip\7z.exe"]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def _inspect_with_7zip(path, env_path=".env"):
    executable = _extractor(env_path)
    if not executable:
        raise RuntimeError("未找到7-Zip，请安装后配置RAR_EXTRACTOR")
    process = subprocess.run([executable, "l", "-slt", "-sccUTF-8", path], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, timeout=60)
    if process.returncode:
        raise ValueError("RAR目录读取失败")
    text = process.stdout.decode("utf-8", "replace")
    records, current = [], {}
    for line in text.splitlines():
        if not line.strip():
            if current.get("Path"):
                records.append(current)
            current = {}
        elif " = " in line:
            key, value = line.split(" = ", 1)
            current[key] = value
    if current.get("Path"):
        records.append(current)
    result = []
    for item in records[1:]:  # first record describes the archive itself
        if item.get("Folder") == "+":
            continue
        result.append({"name": item.get("Path", "").replace("\\", "/"),
                       "size": int(item.get("Size", "0") or 0),
                       "is_link": item.get("Symbolic Link", "") != ""})
    return result


def inspect_archive(path, env_path=".env"):
    files = []
    if rarfile is None:
        entries = _inspect_with_7zip(path, env_path)
    else:
        archive = rarfile.RarFile(path)
        if archive.needs_password():
            archive.close()
            raise ValueError("RAR附件已加密")
        entries = []
        for item in archive.infolist():
            if item.isdir():
                continue
            entries.append({"name": item.filename, "size": item.file_size,
                            "is_link": bool(getattr(item, "is_symlink", lambda: False)())})
        archive.close()
    for item in entries:
            name = item["name"].replace("\\", "/")
            is_link = item.get("is_link", False)
            if (name.startswith("/") or re.match(r"^[A-Za-z]:", name)
                    or ".." in name.split("/") or is_link):
                raise ValueError("RAR包含不安全路径")
            if not name.lower().endswith((".xls", ".xlsx")):
                raise ValueError("RAR包含非Excel文件")
            if item["size"] > MAX_FILE_SIZE:
                raise ValueError("RAR单文件超过100MB")
            files.append({"name": name, "size": item["size"]})
    if len(files) > MAX_FILES:
        raise ValueError("RAR文件数超过500")
    if sum(item["size"] for item in files) > MAX_TOTAL_SIZE:
        raise ValueError("RAR展开总大小超过1GB")
    return files


def _existing_hashes(root):
    values = set()
    if not os.path.isdir(root):
        return values
    for base, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith((".xls", ".xlsx")):
                continue
            try:
                with open(os.path.join(base, name), "rb") as handle:
                    values.add(hashlib.sha256(handle.read()).hexdigest())
            except OSError:
                pass
    return values


def extract_archive(path, output_dir, env_path=".env"):
    listed = inspect_archive(path, env_path)
    executable = _extractor(env_path)
    if not executable:
        raise RuntimeError("未找到7-Zip，请安装后配置RAR_EXTRACTOR")
    os.makedirs(output_dir, exist_ok=True)
    existing = _existing_hashes(output_dir)
    saved, duplicates, ignored = [], 0, []
    with tempfile.TemporaryDirectory(prefix="fof-rar-") as temporary:
        process = subprocess.run([executable, "x", path, "-o%s" % temporary, "-y", "-bd"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=120)
        if process.returncode:
            raise RuntimeError("7-Zip解压失败，退出码%d" % process.returncode)
        for item in listed:
            source = os.path.realpath(os.path.join(temporary, *item["name"].split("/")))
            if not source.startswith(os.path.realpath(temporary) + os.sep) or not os.path.isfile(source):
                raise ValueError("RAR解压结果路径异常")
            filename = os.path.basename(source)
            code_match = re.search(r"\(([A-Za-z0-9]+)\)", filename)
            code = code_match.group(1).upper() if code_match else ""
            if code in EXCLUDED_CODES:
                ignored.append(filename)
                continue
            with open(source, "rb") as handle:
                content = handle.read()
            digest = hashlib.sha256(content).hexdigest()
            if digest in existing:
                duplicates += 1
                continue
            # The archive contains the six parent FOF sheets and their disclosed
            # underlying funds.  Parent sheets use the stable whitelist folder;
            # child sheets are retained by product code for later relationship
            # matching.  The two unrelated 115xx FOF files remain archive-only.
            product_dir = (os.path.join(output_dir, ALLOWED_PRODUCTS[code])
                           if code in ALLOWED_PRODUCTS else
                           os.path.join(output_dir, "四级估值表", code or "待核查"))
            os.makedirs(product_dir, exist_ok=True)
            target = os.path.join(product_dir, filename)
            if os.path.exists(target):
                stem, ext = os.path.splitext(target)
                target = stem + "_" + digest[:8] + ext
            temp_target = target + ".tmp"
            with open(temp_target, "wb") as handle:
                handle.write(content)
            os.replace(temp_target, target)
            existing.add(digest)
            saved.append(os.path.abspath(target))
    return {"saved": saved, "duplicates": duplicates, "ignored": ignored}


def download_underlying_archives(output_dir="底层资产", archive_dir=None,
                                 env_path=".env", latest_only=False):
    root = os.path.dirname(os.path.abspath(output_dir))
    archive_dir = archive_dir or os.path.join(root, ".runtime", "underlying-mail")
    os.makedirs(archive_dir, exist_ok=True)
    manifest_path = os.path.join(archive_dir, "manifest.json")
    manifest = _manifest(manifest_path)
    attachments = manifest.setdefault("attachments", {})
    configured = next((item for item in accounts(env_path)
                       if item["user"].lower() == ACCOUNT), None)
    if not configured:
        raise RuntimeError("未找到QQ邮箱IMAP配置")
    client = None
    result = {"account": ACCOUNT, "sender": SENDER, "checked": 0,
              "archives": [], "downloaded": [], "duplicates": 0,
              "ignored": [], "failures": []}
    try:
        client, insecure = _connect(configured)
        client.login(configured["user"], configured["password"])
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法只读打开QQ收件箱")
        status, data = client.search(None, "ALL")
        ids = data[0].split() if status == "OK" and data else []
        if latest_only:
            ids = ids[-2000:]
        result["checked"] = len(ids)
        result["insecure_tls_fallback"] = insecure
        for message_id in reversed(ids):
            status, payload = client.fetch(message_id, "(BODY.PEEK[])")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1])
            sender = _decode(message.get("From")).lower()
            if SENDER not in sender:
                continue
            for part in message.walk():
                filename = _decode(part.get_filename() or "")
                if not match_archive_attachment(filename):
                    continue
                content = part.get_payload(decode=True) or b""
                digest = hashlib.sha256(content).hexdigest()
                if digest in attachments and attachments[digest].get("status") == "ok":
                    result["duplicates"] += 1
                    continue
                archive_path = os.path.join(archive_dir, digest + ".rar")
                if not os.path.exists(archive_path):
                    temporary = archive_path + ".tmp"
                    with open(temporary, "wb") as handle:
                        handle.write(content)
                    os.replace(temporary, archive_path)
                raw_date = message.get("Date")
                try:
                    mail_date = email.utils.parsedate_to_datetime(raw_date).isoformat()
                except (TypeError, ValueError):
                    mail_date = ""
                record = {"mail_date": mail_date, "subject": _decode(message.get("Subject")),
                          "filename": filename, "sha256": digest, "status": "saved"}
                try:
                    extracted = extract_archive(archive_path, output_dir, env_path)
                    record["status"] = "ok"
                    record.update({"saved": len(extracted["saved"]),
                                   "duplicates": extracted["duplicates"],
                                   "ignored": len(extracted["ignored"])})
                    result["downloaded"].extend(extracted["saved"])
                    result["duplicates"] += extracted["duplicates"]
                    result["ignored"].extend(extracted["ignored"])
                except Exception as exc:
                    record["status"] = "failed"
                    record["error"] = "%s: %s" % (type(exc).__name__, exc)
                    result["failures"].append(record["error"])
                attachments[digest] = record
                result["archives"].append(record)
                _atomic_json(manifest_path, manifest)
    finally:
        if client:
            try:
                client.logout()
            except Exception:
                pass
    return result
