import os
import re
import zipfile
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


def _record(manager, product, primary, secondary, vehicle, source, sheet, row, evidence=None):
    primary = str(primary or "").strip() or "其他"
    if primary == "多策略":
        secondary = str(secondary or "").strip() or "多策略"
    return {"manager": str(manager or "").strip(), "product": str(product or "").strip(),
            "primary": primary, "raw_primary": primary,
            "secondary": str(secondary or "").strip() or "其他",
            "vehicle": str(vehicle or "").strip() or "其他", "source": source,
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
    signatures = {(item["primary"], item["secondary"], item["vehicle"]) for item in candidates}
    if len(signatures) == 1:
        record = candidates[0]
        result = dict(record, match_status="已匹配", match_method=method,
                      confidence="高" if method == "完整名称" else "中")
        result["raw_primary"] = result.get("raw_primary", result["primary"])
        result["primary"], result["classification_basis"] = _effective_stock_primary(result)
        return result
    status = "歧义" if candidates else "其他"
    return {"manager": "", "product": name, "primary": "其他", "raw_primary": "其他", "secondary": "其他",
            "vehicle": "其他", "source": "", "sheet": "", "row": None,
            "normalized": target, "match_status": status, "match_method": "未自动匹配",
            "confidence": "低", "candidate_count": len(candidates),
            "classification_basis": "未匹配或存在歧义，归入其他", "classification_evidence": ""}


def build_label_payload(holding_names, product_labels_path, manager_list_path):
    records, workbooks, errors = load_catalog(product_labels_path, manager_list_path)
    matches = OrderedDict((name, match_label(name, records)) for name in sorted(set(holding_names)))
    return {"matches": matches, "errors": errors, "workbooks": workbooks,
            "record_count": len(records)}
