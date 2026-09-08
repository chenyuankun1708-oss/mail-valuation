import hashlib
import json
import os
import tempfile


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value
    except (OSError, ValueError, TypeError):
        return {} if default is None else default


def atomic_json(path, value):
    absolute = os.path.abspath(path)
    folder = os.path.dirname(absolute)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder,
                                         prefix=".strategy-lab-", suffix=".tmp",
                                         delete=False) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary, absolute)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
