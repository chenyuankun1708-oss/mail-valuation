import os
import re
import zipfile
import json
import hashlib
import shutil
import uuid
from datetime import datetime
from collections import OrderedDict
from xml.etree import ElementTree as ET

import openpyxl


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
GENERIC = ("私募证券投资基金", "私募投资基金", "证券投资基金", "集合资产管理计划",
           "单一资产管理计划", "资产管理计划", "私募基金", "投资基金")
CLASS_SUFFIX = re.compile(r"(?:A|B|C|D|E|R|I|H|A类|B类|C类|D类|份额)$", re.I)


def _column_number(reference):
    letters = re.match(r"[A-Z]+", reference or "A").group()
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - 64
    return value


def _xml_workbook(path):
    """Read cell values without loading the workbook's sometimes-invalid styles.xml."""
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.findall(".//{%s}t" % MAIN_NS))
                      for item in root.findall("{%s}si" % MAIN_NS)]
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {node.attrib["Id"]: node.attrib["Target"]
                   for node in relationships.findall("{%s}Relationship" % PKG_NS)}
        result = OrderedDict()
        for sheet in workbook.findall(".//{%s}sheet" % MAIN_NS):
            target = targets[sheet.attrib["{%s}id" % REL_NS]]
            if not target.startswith("xl/"):
                target = "xl/" + target.lstrip("/")
            root = ET.fromstring(archive.read(target))
            rows = []
            for row_node in root.findall(".//{%s}sheetData/{%s}row" % (MAIN_NS, MAIN_NS)):
                values = {}
                for cell in row_node.findall("{%s}c" % MAIN_NS):
                    number = _column_number(cell.attrib.get("r"))
                    value_node = cell.find("{%s}v" % MAIN_NS)
                    value = None if value_node is None else value_node.text
                    if cell.attrib.get("t") == "s" and value is not None:
                        value = shared[int(value)]
                    elif cell.attrib.get("t") == "inlineStr":
                        value = "".join(node.text or "" for node in cell.findall(".//{%s}t" % MAIN_NS))
                    values[number] = value
                width = max(values) if values else 0
                rows.append([values.get(index) for index in range(1, width + 1)])
            result[sheet.attrib["name"]] = rows
        return result


def read_workbook(path):
    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return OrderedDict((sheet.title, [[cell.value for cell in row] for row in sheet.iter_rows()])
                               for sheet in book.worksheets)
        finally:
            book.close()
    except Exception:
        return _xml_workbook(path)


def normalize_name(value):
    text = str(value or "").upper()
    text = re.sub(r"[\s·•（）()【】\[\]，,。._\-—_/\\'\"“”]+", "", text)
    for suffix in GENERIC:
        text = text.replace(suffix.upper(), "")
    previous = None
    while previous != text:
        previous = text
        text = CLASS_SUFFIX.sub("", text)
    return text


def _manager_key(value):
    return normalize_name(value).replace("投资管理", "").replace("资产管理", "").replace("投资", "")


def _record(manager, product, primary, secondary, vehicle, source, sheet, row, evidence=None,
            department=None):
    primary = str(primary or "").strip() or "其他"
    if primary == "多策略":
        secondary = str(secondary or "").strip() or "多策略"
    return {"manager": str(manager or "").strip(), "product": str(product or "").strip(),
            "primary": primary, "raw_primary": primary,
            "secondary": str(secondary or "").strip() or "其他",
            "vehicle": str(vehicle or "").strip() or "其他",
            "department": str(department or "").strip() or "无", "source": source,
            "sheet": sheet, "row": row, "normalized": normalize_name(product),
            "classification_evidence": str(evidence or "").strip()}


def _effective_stock_primary(record):
    raw = record.get("raw_primary", record.get("primary", "其他"))
    if raw != "股票":
        return raw, "原始一级标签"
    evidence = "%s %s" % (record.get("secondary", ""), record.get("classification_evidence", ""))
    if any(word in evidence for word in ("灵活对冲", "多空对冲", "不完全对冲")):
        return "股票对冲", "二级标签/策略描述/敞口包含灵活、多空或不完全对冲"
    if "中性" in evidence or "完全对冲" in evidence:
        return "股票中性", "二级标签/策略描述/敞口包含中性或完全对冲"
    return "股票指增", "股票类未显示中性或非完全对冲，按股票多头/指增归类"


def load_catalog(product_labels_path, manager_list_path):
    records, errors, workbooks = [], [], {}
    for kind, path in (("产品标签", product_labels_path), ("管理人清单", manager_list_path)):
        try:
            workbooks[kind] = read_workbook(path)
        except Exception as exc:
            workbooks[kind] = OrderedDict()
            errors.append({"source": kind, "error": "%s读取失败（%s）" % (kind, type(exc).__name__)})
    manager_book = workbooks.get("管理人清单", {})
    for row_number, row in enumerate(manager_book.get("策略归类拆分", [])[2:], 3):
        def get(column):
            return row[column - 1] if len(row) >= column else None
        if get(9):
            records.append(_record(get(2), get(9), get(82), get(83), get(11),
                                   "管理人清单", "策略归类拆分", row_number, get(10)))
    for sheet_name, rows in workbooks.get("产品标签", {}).items():
        if not rows:
            continue
        header = {str(value or "").strip(): index for index, value in enumerate(rows[0])}
        for row_number, row in enumerate(rows[1:], 2):
            def value(label):
                index = header.get(label)
                return row[index] if index is not None and index < len(row) else None
            if value("产品名称"):
                records.append(_record(value("管理人"), value("产品名称"), value("一级标签") or sheet_name,
                                       value("二级标签"), None, "产品标签", sheet_name, row_number,
                                       value("是否有敞口")))
    return records, workbooks, errors


LABEL_SCHEMA_VERSION = 1


def _stable_id(prefix, *parts):
    value = "|".join(str(part or "") for part in parts).encode("utf-8")
    return "%s_%s" % (prefix, hashlib.sha256(value).hexdigest()[:16])


def migrate_catalog(product_labels_path, manager_list_path, json_path, now=None):
    """Migrate the two historical workbooks into the local authoritative JSON file."""
    records, _workbooks, errors = load_catalog(product_labels_path, manager_list_path)
    if errors:
        raise ValueError("；".join(item["error"] for item in errors))
    timestamp = (now or datetime.now()).replace(microsecond=0).isoformat()
    migrated = []
    for record in records:
        item = dict(record)
        item["department"] = str(item.get("department") or "").strip() or "无"
        item["product_id"] = _stable_id("product", item.get("normalized"))
        item["record_id"] = _stable_id(
            "label", item.get("normalized"), item.get("source"), item.get("sheet"), item.get("row")
        )
        item["classification_basis"] = item.pop("classification_evidence", "")
        item["active"] = True
        item["version"] = 1
        item["updated_at"] = timestamp
        migrated.append(item)
    payload = {
        "schema_version": LABEL_SCHEMA_VERSION,
        "revision": 1,
        "updated_at": timestamp,
        "migrated_from": [os.path.basename(product_labels_path), os.path.basename(manager_list_path)],
        "records": migrated,
    }
    folder = os.path.dirname(os.path.abspath(json_path))
    if not os.path.isdir(folder):
        os.makedirs(folder)
    temporary = json_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, json_path)
    return payload


def load_json_catalog(json_path):
    with open(json_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != LABEL_SCHEMA_VERSION:
        raise ValueError("不支持的标签JSON版本")
    records = []
    for source in payload.get("records", []):
        if not source.get("active", True):
            continue
        item = dict(source)
        item["classification_evidence"] = item.get("classification_basis", "")
        item["normalized"] = item.get("normalized") or normalize_name(item.get("product"))
        item["department"] = str(item.get("department") or "").strip() or "无"
        records.append(item)
    return records, payload


_LABEL_DEFAULTS = {"manager": "", "primary": "其他", "secondary": "其他",
                   "vehicle": "其他", "department": "无",
                   "classification_basis": "", "classification_evidence": ""}


def deduplicate_label_records(records):
    """Return one effective active record per normalized product name and an audit check."""
    groups = OrderedDict()
    for position, source in enumerate(records):
        item = dict(source)
        item["normalized"] = item.get("normalized") or normalize_name(item.get("product"))
        item["department"] = str(item.get("department") or "").strip() or "无"
        item["classification_evidence"] = item.get(
            "classification_evidence", item.get("classification_basis", ""))
        key = item["normalized"] or "__blank_%s" % position
        groups.setdefault(key, []).append((position, item))

    effective = []
    duplicates = []
    merge_fields = ("manager", "primary", "secondary", "vehicle", "department",
                    "classification_basis", "classification_evidence")
    for normalized, members in groups.items():
        def score(pair):
            position, item = pair
            meaningful = sum(1 for field in merge_fields
                             if str(item.get(field) or "").strip() not in
                             ("", _LABEL_DEFAULTS.get(field, "")))
            manager_source = 1 if item.get("source") == "管理人清单" else 0
            return meaningful, manager_source, -position

        _position, canonical_source = max(members, key=score)
        canonical = dict(canonical_source)
        conflicts = []
        for field in merge_fields:
            default = _LABEL_DEFAULTS.get(field, "")
            meaningful_values = []
            for _member_position, item in members:
                value = str(item.get(field) or "").strip()
                if value and value != default and value not in meaningful_values:
                    meaningful_values.append(value)
            if len(meaningful_values) == 1:
                canonical[field] = meaningful_values[0]
            elif len(meaningful_values) > 1:
                conflicts.append({"field": field, "values": meaningful_values})
        if canonical.get("primary"):
            canonical["raw_primary"] = canonical.get("raw_primary") or canonical["primary"]
        record_ids = [item.get("record_id") for _member_position, item in members
                      if item.get("record_id")]
        canonical["duplicate_record_ids"] = record_ids
        effective.append(canonical)
        if len(members) > 1:
            duplicates.append({
                "normalized_name": normalized,
                "product_name": canonical.get("product") or normalized,
                "record_count": len(members),
                "record_ids": record_ids,
                "canonical_record_id": canonical.get("record_id"),
                "conflicts": conflicts,
            })
    check = {"checked_records": len(records), "unique_product_names": len(effective),
             "duplicate_groups": len(duplicates), "duplicates": duplicates}
    return effective, check


def ensure_catalog_products(json_path, holding_names, audit_path=None, now=None):
    """Add previously unseen end-position names with neutral labels, once per normalized name."""
    with open(json_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("schema_version") != LABEL_SCHEMA_VERSION:
        raise ValueError("不支持的标签JSON版本")
    records = payload.setdefault("records", [])
    known = {normalize_name(item.get("product")) for item in records
             if normalize_name(item.get("product"))}
    additions = []
    timestamp = (now or datetime.now()).replace(microsecond=0).isoformat()
    for name in holding_names:
        product = str(name or "").strip()
        normalized = normalize_name(product)
        if not normalized or normalized in known:
            continue
        item = {
            "product_id": _stable_id("product", normalized),
            "record_id": "label_%s" % uuid.uuid4().hex[:16],
            "normalized": normalized,
            "manager": "",
            "product": product,
            "primary": "其他",
            "raw_primary": "其他",
            "secondary": "其他",
            "vehicle": "其他",
            "department": "无",
            "classification_basis": "期末底层持仓自动补录，待人工维护",
            "source": "估值表自动补录",
            "sheet": "",
            "row": None,
            "active": True,
            "version": 1,
            "updated_at": timestamp,
        }
        records.append(item)
        additions.append(item)
        known.add(normalized)
    if not additions:
        return payload, []

    current_revision = int(payload.get("revision", 0))
    payload["revision"] = current_revision + 1
    payload["updated_at"] = timestamp
    backup_dir = os.path.join(os.path.dirname(json_path), "label_backups")
    if not os.path.isdir(backup_dir):
        os.makedirs(backup_dir)
    shutil.copy2(json_path, os.path.join(
        backup_dir, "product_labels.r%s.json" % current_revision))
    temporary = json_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, json_path)
    if audit_path:
        audit_folder = os.path.dirname(os.path.abspath(audit_path))
        if not os.path.isdir(audit_folder):
            os.makedirs(audit_folder)
        audit = {"timestamp": timestamp, "user": "system:auto-label-sync",
                 "action": "auto_create_missing_end_holdings",
                 "products": [item["product"] for item in additions],
                 "record_ids": [item["record_id"] for item in additions],
                 "revision_before": current_revision,
                 "revision_after": payload["revision"]}
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
    return payload, additions


EDITABLE_FIELDS = ("product", "manager", "primary", "secondary", "vehicle", "department",
                   "classification_basis", "active")


class LabelConflictError(Exception):
    pass


def _validated_changes(values, creating=False):
    unknown = set(values) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError("不允许的标签字段：%s" % ", ".join(sorted(unknown)))
    result = {}
    for key in EDITABLE_FIELDS:
        if key not in values:
            continue
        value = values[key]
        if key == "active":
            if not isinstance(value, bool):
                raise ValueError("active必须为布尔值")
            result[key] = value
        else:
            if not isinstance(value, str):
                raise ValueError("%s必须为文本" % key)
            value = value.strip()
            if len(value) > 500:
                raise ValueError("%s过长" % key)
            result[key] = value
    if creating and not result.get("product"):
        raise ValueError("产品名称不能为空")
    for key in ("primary", "secondary", "vehicle"):
        if key in result and not result[key]:
            result[key] = "其他"
    if "department" in result and not result["department"]:
        result["department"] = "无"
    return result


def mutate_catalog(json_path, action, values, expected_revision, username,
                   record_id=None, audit_path=None, now=None):
    """Apply one optimistic, audited mutation to the authoritative label JSON."""
    with open(json_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    current_revision = int(payload.get("revision", 0))
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError):
        raise ValueError("缺少有效expected_revision")
    if expected_revision != current_revision:
        raise LabelConflictError("标签已被其他页面更新，请刷新后重试")
    timestamp = (now or datetime.now()).replace(microsecond=0).isoformat()
    records = payload.setdefault("records", [])
    before = None
    if action == "create":
        changes = _validated_changes(values, creating=True)
        normalized = normalize_name(changes["product"])
        if any(normalize_name(row.get("product")) == normalized for row in records):
            raise ValueError("产品名称已存在，请编辑或重新启用原记录")
        item = {"product_id": _stable_id("product", normalized),
                "record_id": "label_%s" % uuid.uuid4().hex[:16],
                "normalized": normalized, "source": "网页标签", "sheet": "", "row": None,
                "raw_primary": changes.get("primary", "其他"), "version": 1,
                "updated_at": timestamp, "active": True}
        item.update({"manager": "", "product": "", "primary": "其他", "secondary": "其他",
                     "vehicle": "其他", "department": "无", "classification_basis": ""})
        item.update(changes)
        records.append(item)
    elif action in ("update", "deactivate"):
        item = next((row for row in records if row.get("record_id") == record_id), None)
        if item is None:
            raise KeyError("标签记录不存在")
        before = dict(item)
        changes = {"active": False} if action == "deactivate" else _validated_changes(values)
        if "product" in changes:
            changed_normalized = normalize_name(changes["product"])
            if any(row is not item and normalize_name(row.get("product")) == changed_normalized
                   for row in records):
                raise ValueError("产品名称已存在，请编辑或重新启用原记录")
        item.update(changes)
        if "product" in changes:
            item["normalized"] = normalize_name(item["product"])
            item["product_id"] = _stable_id("product", item["normalized"])
        if "primary" in changes:
            item["raw_primary"] = changes["primary"]
        item["version"] = int(item.get("version", 0)) + 1
        item["updated_at"] = timestamp
    else:
        raise ValueError("不支持的标签操作")
    payload["revision"] = current_revision + 1
    payload["updated_at"] = timestamp
    backup_dir = os.path.join(os.path.dirname(json_path), "label_backups")
    if not os.path.isdir(backup_dir):
        os.makedirs(backup_dir)
    backup_path = os.path.join(backup_dir, "product_labels.r%s.json" % current_revision)
    shutil.copy2(json_path, backup_path)
    temporary = json_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, json_path)
    if audit_path:
        audit_folder = os.path.dirname(os.path.abspath(audit_path))
        if not os.path.isdir(audit_folder):
            os.makedirs(audit_folder)
        audit = {"timestamp": timestamp, "user": username, "action": action,
                 "record_id": item["record_id"], "product": item.get("product"),
                 "revision_before": current_revision, "revision_after": payload["revision"],
                 "before": before, "after": item}
        with open(audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(audit, ensure_ascii=False) + "\n")
    return payload, item


def catalog_workbook(records):
    headers = ["产品稳定标识", "记录标识", "产品名称", "管理人", "一级标签", "二级标签",
               "投资形式", "营业部", "分类依据", "启用", "版本", "更新时间", "原始来源"]
    rows = [headers]
    for item in records:
        rows.append([item.get("product_id"), item.get("record_id"), item.get("product"),
                     item.get("manager"), item.get("primary"), item.get("secondary"),
                     item.get("vehicle"), item.get("department") or "无", item.get("classification_basis"),
                     item.get("active", True),
                     item.get("version"), item.get("updated_at"), item.get("source")])
    return {"线上标签JSON": {"有效标签": rows}}


def _long_enough(value):
    chinese = len(re.findall(r"[\u4e00-\u9fff]", value))
    alphanumeric = len(re.findall(r"[A-Z0-9]", value))
    return chinese >= 4 or alphanumeric >= 6


def match_label(name, records):
    target = normalize_name(name)
    exact = [record for record in records if record["normalized"] and record["normalized"] == target]
    candidates = exact
    method = "完整名称"
    if not candidates:
        candidates = [record for record in records if _long_enough(record["normalized"]) and
                      (record["normalized"] in target or target in record["normalized"])]
        manager_consistent = [record for record in candidates
                              if _manager_key(record.get("manager")) and
                              _manager_key(record.get("manager")) in target]
        if manager_consistent:
            candidates = manager_consistent
            method = "管理人一致的简称"
        else:
            method = "唯一简称"
    if candidates:
        best_length = max(len(record["normalized"]) for record in candidates)
        candidates = [record for record in candidates if len(record["normalized"]) == best_length]
        # The manager-list invested-product record has priority over the generic catalogue.
        preferred = [record for record in candidates if record["source"] == "管理人清单"]
        if preferred:
            manager_record = dict(preferred[0])
            supplements = [record for record in candidates if record["source"] == "产品标签"]
            usable = [record for record in supplements if record["primary"] != "其他"]
            supplement_signatures = {(record["primary"], record["secondary"]) for record in usable}
            if manager_record["primary"] == "其他" and len(supplement_signatures) == 1:
                supplement = usable[0]
                manager_record["primary"] = supplement["primary"]
                manager_record["raw_primary"] = supplement.get("raw_primary", supplement["primary"])
                manager_record["secondary"] = supplement["secondary"]
                manager_record["source"] = "管理人清单+产品标签"
                manager_record["sheet"] = "%s + %s" % (manager_record["sheet"], supplement["sheet"])
                manager_record["classification_evidence"] = "；".join(filter(None, (
                    manager_record.get("classification_evidence"),
                    supplement.get("classification_evidence"))))
            candidates = [manager_record]
    signatures = {(item["primary"], item["secondary"], item["vehicle"],
                   item.get("department") or "无") for item in candidates}
    if len(signatures) == 1:
        record = candidates[0]
        result = dict(record, match_status="已匹配", match_method=method,
                      confidence="高" if method == "完整名称" else "中")
        result["raw_primary"] = result.get("raw_primary", result["primary"])
        result["primary"], result["classification_basis"] = _effective_stock_primary(result)
        return result
    status = "歧义" if candidates else "其他"
    return {"manager": "", "product": name, "primary": "其他", "raw_primary": "其他", "secondary": "其他",
            "vehicle": "其他", "department": "无", "source": "", "sheet": "", "row": None,
            "normalized": target, "match_status": status, "match_method": "未自动匹配",
            "confidence": "低", "candidate_count": len(candidates),
            "classification_basis": "未匹配或存在歧义，归入其他", "classification_evidence": ""}


def build_label_payload(holding_names, json_path, auto_add_names=None, audit_path=None):
    errors = []
    try:
        additions = []
        if auto_add_names is not None:
            _catalog, additions = ensure_catalog_products(
                json_path, auto_add_names, audit_path=audit_path)
        records, catalog = load_json_catalog(json_path)
    except Exception as exc:
        records, catalog, additions = [], {"records": [], "revision": None, "updated_at": None}, []
        errors.append({"source": "线上标签JSON", "error": "标签JSON读取失败（%s）" % type(exc).__name__})
    effective_records, dedupe_check = deduplicate_label_records(records)
    matches = OrderedDict((name, match_label(name, effective_records))
                          for name in sorted(set(holding_names)))
    return {"matches": matches, "errors": errors,
            "workbooks": catalog_workbook(effective_records),
            "records": catalog.get("records", []), "deduped_records": effective_records,
            "dedupe_check": dedupe_check,
            "auto_added_products": [item.get("product") for item in additions],
            "record_count": len(effective_records), "raw_record_count": len(records),
            "revision": catalog.get("revision"), "updated_at": catalog.get("updated_at")}
