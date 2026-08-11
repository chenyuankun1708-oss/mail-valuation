from collections import OrderedDict


PRODUCTS = OrderedDict([
    ("第一创业天玑13号单一资产管理计划", ("天玑13号", "天玑13", "SALT58")),
    ("国金资管盛乾同行2号FOF单一资产管理计划", ("盛乾同行2号", "盛乾同行2")),
    ("第一创业尊享FOF55号单一资产管理计划", ("尊享FOF55号", "SATQ22")),
    ("招商资管元盈FOF1号单一资产管理计划", ("元盈FOF1号", "6430")),
    ("第一创业源泉优享FOF3号单一资产管理计划", ("源泉优享FOF3号", "源泉优享FOF3", "SBBB51")),
    ("国金资管盛乾同行5号FOF单一资产管理计划", ("盛乾同行5号",)),
    ("国泰君安君得3640单一资产管理计划", ("君得3640",)),
    ("中信证券资管财富私享投资3791号FOF单一资产管理计划", ("3791号FOF", "3791")),
    ("中金财富私享1554号FOF单一资产管理计划", ("私享1554号", "6OH8LF")),
    ("中金财富私享11518号FOF单一资产管理计划", ("私享11518号", "SBPR95")),
    ("中金财富私享11598号FOF单一资产管理计划", ("私享11598号", "SBPR93")),
    ("国泰海通私客尊享FOF8075号单一资产管理计划", ("私客尊享FOF8075", "8075")),
    ("中信证券资管财富私享投资5588号FOF单一资产管理计划", ("5588号FOF", "5588")),
    ("西南证券嘉盈1号FOF单一资产管理计划", ("嘉盈1号FOF",)),
    ("中信建投聚智多策略9号FOF单一资产管理计划", ("聚智多策略9号",)),
    ("五矿证券FOF50号单一资产管理计划", ("五矿证券FOF50号", "FOF50号")),
    ("五矿证券FOF51号单一资产管理计划", ("五矿证券FOF51号", "FOF51号")),
])


def match_product(text):
    normalized = str(text).replace(" ", "").upper()
    matches = []
    for name, aliases in PRODUCTS.items():
        if any(alias.replace(" ", "").upper() in normalized for alias in (name,) + aliases):
            matches.append((max(len(a) for a in (name,) + aliases if a.replace(" ", "").upper() in normalized), name))
    return max(matches)[1] if matches else None
