import hashlib
import os
import shutil

from .parser import parse_valuation


def organize_products(root="products"):
    """复制可识别估值表到标准产品目录；原始估值表保持只读且不删除。"""
    root = os.path.abspath(root)
    files = []
    for base, _, names in os.walk(root):
        for name in names:
            if name.lower().endswith((".xls", ".xlsx")) and not name.startswith("~$"):
                files.append(os.path.join(base, name))
    copied, duplicates, errors = [], [], []
    for source in files:
        try:
            snapshot = parse_valuation(source)
            target_dir = os.path.abspath(os.path.join(root, snapshot.product))
            if os.path.commonpath((root, target_dir)) != root:
                raise ValueError("归档路径越界")
            # 已位于标准产品目录（含其子目录）的文件无需再次归档。
            if os.path.commonpath((source, target_dir)) == target_dir:
                continue
            clean_name = os.path.basename(source)
            prefix = snapshot.valuation_date + "_"
            if not clean_name.startswith(prefix):
                clean_name = prefix + clean_name
            target = os.path.join(target_dir, clean_name)
            if os.path.normcase(source) == os.path.normcase(target):
                continue
            os.makedirs(target_dir, exist_ok=True)
            if os.path.exists(target):
                with open(source, "rb") as left, open(target, "rb") as right:
                    same = hashlib.sha256(left.read()).digest() == hashlib.sha256(right.read()).digest()
                if same:
                    duplicates.append(source)
                    continue
                stem, ext = os.path.splitext(target)
                with open(source, "rb") as handle:
                    suffix = hashlib.sha256(handle.read()).hexdigest()[:8]
                target = stem + "_副本_" + suffix + ext
            shutil.copy2(source, target)
            copied.append({"from": source, "to": target})
        except Exception as exc:
            errors.append({"file": source, "error": str(exc)})
    return {"copied": copied, "duplicates_skipped": duplicates, "errors": errors}
