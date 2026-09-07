import io
import os
import zipfile
from datetime import datetime

from .parser import scan_valuations


def build_valuation_archive(products_dir, valuation_date):
    """Return a ZIP containing the deduplicated source valuations for one exact date."""
    try:
        parsed_date = datetime.strptime(valuation_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError("请选择有效估值日期")
    if parsed_date.isoformat() != valuation_date:
        raise ValueError("请选择有效估值日期")

    products_root = os.path.realpath(products_dir)
    snapshots, _ = scan_valuations(products_root)
    selected = [item for item in snapshots if item.valuation_date == valuation_date]
    if not selected:
        raise ValueError("所选日期没有可用估值表")

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for snapshot in sorted(selected, key=lambda item: (item.product, item.source_file)):
            source = os.path.realpath(snapshot.source_file)
            if os.path.commonpath([products_root, source]) != products_root:
                raise ValueError("估值表路径不在产品目录内")
            archive.write(source, os.path.join(snapshot.product, os.path.basename(source)))
    return output.getvalue(), len(selected)
