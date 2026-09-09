"""华银元鼎月利系列独立估值报告。

本文件与主项目17只FOF的白名单、index.html和自动刷新流程完全隔离。
它只读搜索已在本机.env配置的邮箱，将匹配附件保存到
``standalone_huayin/raw``，并生成 ``standalone_huayin/huayin_report.html``。
邮箱密码不会写入输出、日志或源码。
"""
import argparse
import email
import hashlib
import html
import json
import math
import os
import re
from datetime import date, datetime

from valuation_app.analytics import _analyze_product
from valuation_app.mail import _connect, _decode, accounts
from valuation_app.parser import parse_valuation


OUTPUT_DIR = "standalone_huayin"
RAW_DIR = os.path.join(OUTPUT_DIR, "raw")
REPORT_PATH = os.path.join(OUTPUT_DIR, "huayin_report.html")
MATCH_RE = re.compile(r"(?:华银.*?元鼎.*?月利|元鼎.*?月利|华银元鼎)", re.I)
PRODUCT_RE = re.compile(r"华银元鼎月利[^\s_（）()]*号")


def _safe_filename(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "估值表.xls"


def search_mail(env_path=".env", latest_only=False):
    configured = accounts(env_path)
    if not configured:
        raise RuntimeError("未找到可用邮箱配置")
    os.makedirs(RAW_DIR, exist_ok=True)
    known = set()
    for filename in os.listdir(RAW_DIR):
        path = os.path.join(RAW_DIR, filename)
        if os.path.isfile(path):
            with open(path, "rb") as handle:
                known.add(hashlib.sha256(handle.read()).hexdigest())
    found, failures = [], []
    for account in configured:
        client = None
        try:
            client, _ = _connect(account)
            client.login(account["user"], account["password"])
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("无法只读打开收件箱")
            status, data = client.search(None, "ALL")
            if status != "OK" or not data:
                raise RuntimeError("邮箱搜索失败")
            ids = data[0].split()
            if latest_only:
                ids = ids[-2000:]
            matched = 0
            for message_id in reversed(ids):
                status, payload = client.fetch(message_id, "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                message = email.message_from_bytes(payload[0][1])
                subject = _decode(message.get("Subject"))
                for part in message.walk():
                    raw_name = part.get_filename()
                    if not raw_name:
                        continue
                    filename = _decode(raw_name)
                    searchable = "%s %s" % (subject, filename)
                    if not MATCH_RE.search(searchable) or not filename.lower().endswith((".xls", ".xlsx")):
                        continue
                    content = part.get_payload(decode=True)
                    if not content:
                        continue
                    matched += 1
                    digest = hashlib.sha256(content).hexdigest()
                    raw_date = message.get("Date")
                    try:
                        mail_date = email.utils.parsedate_to_datetime(raw_date).strftime("%Y%m%d")
                    except (TypeError, ValueError, AttributeError):
                        mail_date = "unknown"
                    target = os.path.join(RAW_DIR, _safe_filename(mail_date + "_" + filename))
                    saved = False
                    if digest not in known:
                        if os.path.exists(target):
                            stem, ext = os.path.splitext(target)
                            target = stem + "_" + digest[:8] + ext
                        with open(target, "wb") as handle:
                            handle.write(content)
                        known.add(digest)
                        saved = True
                    found.append({"account": account["user"], "subject": subject,
                                  "filename": filename, "saved": saved,
                                  "path": os.path.abspath(target) if saved else None})
            print("%s：检查%d封，匹配%d个附件" % (account["user"], len(ids), matched))
        except Exception as exc:
            failures.append("%s：%s" % (account["user"], type(exc).__name__))
        finally:
            if client:
                try:
                    client.logout()
                except Exception:
                    pass
    return found, failures


def _risk(points):
    clean = [p for p in points if p.accumulated_nav and p.accumulated_nav > 0]
    if len(clean) < 2:
        return {"cagr": None, "drawdown": None, "volatility": None, "sharpe": None}
    days = (date.fromisoformat(clean[-1].valuation_date) - date.fromisoformat(clean[0].valuation_date)).days
    cagr = (clean[-1].accumulated_nav / clean[0].accumulated_nav) ** (365.0 / days) - 1 if days else None
    peak, drawdown = clean[0].accumulated_nav, 0.0
    daily = []
    for previous, current in zip(clean, clean[1:]):
        peak = max(peak, current.accumulated_nav)
        drawdown = min(drawdown, current.accumulated_nav / peak - 1)
        gap = (date.fromisoformat(current.valuation_date) - date.fromisoformat(previous.valuation_date)).days
        if gap > 0:
            daily.append((math.log(current.accumulated_nav / previous.accumulated_nav) / gap, gap))
    volatility = sharpe = None
    if len(daily) >= 2:
        weight = sum(gap for _, gap in daily)
        mean = sum(value * gap for value, gap in daily) / weight
        variance = sum(gap * (value - mean) ** 2 for value, gap in daily) / max(weight - 1, 1)
        volatility = math.sqrt(max(variance, 0) * 365)
        if volatility > 1e-12:
            sharpe = (mean * 365 - math.log(1.02)) / volatility
    return {"cagr": cagr, "drawdown": drawdown, "volatility": volatility, "sharpe": sharpe}


def parse_files():
    snapshots, errors = [], []
    if not os.path.isdir(RAW_DIR):
        return snapshots, errors
    hashes = set()
    for filename in sorted(os.listdir(RAW_DIR)):
        if not filename.lower().endswith((".xls", ".xlsx")):
            continue
        path = os.path.join(RAW_DIR, filename)
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).digest()
            if digest in hashes:
                continue
            hashes.add(digest)
            name_match = PRODUCT_RE.search(filename)
            product = name_match.group(0) if name_match else "华银元鼎月利X号"
            snapshots.append(parse_valuation(path, product=product))
        except Exception as exc:
            errors.append({"file": filename, "error": "%s: %s" % (type(exc).__name__, str(exc))})
    unique = {}
    for item in snapshots:
        key = (item.product, item.valuation_date)
        old = unique.get(key)
        if old is None or len(item.holdings) > len(old.holdings):
            unique[key] = item
    return sorted(unique.values(), key=lambda x: (x.product, x.valuation_date)), errors


def build_report(snapshots, errors, mail_failures=None):
    grouped = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.product, []).append(snapshot)
    products = []
    for name, series in sorted(grouped.items()):
        series.sort(key=lambda x: x.valuation_date)
        result = _analyze_product(name, series)
        result["risk"] = _risk(series)
        products.append(result)
    payload = {"generated_at": datetime.now().replace(microsecond=0).isoformat(),
               "products": products, "parse_errors": errors,
               "mail_failures": mail_failures or []}
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    page = """<!doctype html><meta charset=utf-8><title>华银元鼎月利系列独立分析</title>
<style>body{margin:0;background:#f3f5f8;color:#17202a;font:14px/1.55 system-ui,'Microsoft YaHei';}.wrap{max-width:1280px;margin:auto;padding:28px}h1{margin:0}.sub{color:#718096}.panel{background:#fff;border:1px solid #dde3ea;border-radius:14px;padding:18px;margin:16px 0}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:#f7f9fb;border-radius:10px;padding:13px}.card b{display:block;font-size:21px;margin-top:4px}.pos{color:#c84435}.neg{color:#08765b}select,input,button{padding:8px;border:1px solid #ccd5df;border-radius:7px}button{background:#173b6c;color:white}canvas{width:100%;height:320px;border:1px solid #e1e6ec;border-radius:8px}table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #e6ebf0;text-align:right}th:first-child,td:first-child{text-align:left}.warn{background:#fff3d9;border-left:3px solid #c6923c;padding:10px}@media(max-width:800px){.cards{grid-template-columns:1fr 1fr}.wrap{padding:14px}}</style>
<div class=wrap><h1>华银元鼎月利系列独立分析</h1><p class=sub>独立文件，不属于现有FOF投资驾驶舱。金额现金流按估值表份额变化近似识别；没有资金台账时，不应视为已确认申赎流水。</p><div class=panel><label>产品 <select id=product></select></label> <label>开始 <input id=start type=date></label> <label>结束 <input id=end type=date></label> <button onclick=render()>计算</button></div><div id=content></div></div>
<script>const RAW=""" + data + """;const money=v=>v==null?'—':'¥'+Number(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});const pct=v=>v==null?'—':Number(v).toFixed(2)+'%';const cc=v=>v>0?'pos':v<0?'neg':'';function render(){const p=RAW.products.find(x=>x.name===product.value);if(!p)return;const pts=p.points.filter(x=>x.date>=start.value&&x.date<=end.value);if(pts.length<2){content.innerHTML='<div class="panel warn">所选区间至少需要两个估值日。</div>';return}const a=pts[0],b=pts[pts.length-1],days=(new Date(b.date)-new Date(a.date))/86400000,ret=(b.accumulated_nav/a.accumulated_nav-1)*100,cagr=(Math.pow(b.accumulated_nav/a.accumulated_nav,365/days)-1)*100;let peak=a.accumulated_nav,dd=0;pts.forEach(x=>{peak=Math.max(peak,x.accumulated_nav);dd=Math.min(dd,x.accumulated_nav/peak-1)});content.innerHTML=`<div class=cards><div class=card>实际区间<b>${a.date}<br>${b.date}</b></div><div class=card>期末资产<b>${money(b.net_assets)}</b></div><div class=card>累计净值收益率<b class=${cc(ret)}>${pct(ret)}</b></div><div class=card>净值年化收益率<b class=${cc(cagr)}>${pct(cagr)}</b></div><div class=card>最大回撤<b class=neg>${pct(dd*100)}</b></div><div class=card>估值点数<b>${pts.length}</b></div><div class=card>全历史估算金额收益<b class=${cc(p.profit)}>${money(p.profit)}</b></div><div class=card>全历史时间加权收益<b class=${cc(p.time_weighted_return_pct)}>${pct(p.time_weighted_return_pct)}</b></div></div><div class=panel><canvas id=chart width=1160 height=320></canvas></div><div class=panel><table><thead><tr><th>估值日</th><th>单位净值</th><th>累计单位净值</th><th>资产净值</th><th>份额</th></tr></thead><tbody>${pts.map(x=>`<tr><td>${x.date}</td><td>${x.nav.toFixed(6)}</td><td>${x.accumulated_nav.toFixed(6)}</td><td>${money(x.net_assets)}</td><td>${Number(x.shares).toLocaleString('zh-CN')}</td></tr>`).join('')}</tbody></table></div>`;draw(pts)}function draw(pts){const c=chart,x=c.getContext('2d'),L=55,R=20,T=20,B=35,vals=pts.map(p=>p.accumulated_nav),lo=Math.min(...vals),hi=Math.max(...vals),span=hi-lo||1;x.clearRect(0,0,c.width,c.height);x.strokeStyle='#1b7f6b';x.lineWidth=2;x.beginPath();pts.forEach((p,i)=>{const xx=L+i/Math.max(pts.length-1,1)*(c.width-L-R),yy=T+(hi-p.accumulated_nav)/span*(c.height-T-B);i?x.lineTo(xx,yy):x.moveTo(xx,yy)});x.stroke()}product.innerHTML=RAW.products.map(p=>`<option>${p.name}</option>`).join('');if(RAW.products.length){const p=RAW.products[0];start.value=p.points[0].date;end.value=p.points[p.points.length-1].date;render()}else content.innerHTML='<div class="panel warn">尚未找到并成功解析华银元鼎月利系列估值表。</div>';</script>"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    temporary = REPORT_PATH + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(page)
    os.replace(temporary, REPORT_PATH)
    return os.path.abspath(REPORT_PATH), products


def main():
    parser = argparse.ArgumentParser(description="华银元鼎月利系列独立估值报告")
    parser.add_argument("--no-mail", action="store_true", help="只用已经下载的本地附件重建")
    parser.add_argument("--latest-only", action="store_true", help="每个邮箱只检查最近2000封")
    args = parser.parse_args()
    found, failures = ([], []) if args.no_mail else search_mail(latest_only=args.latest_only)
    snapshots, errors = parse_files()
    path, products = build_report(snapshots, errors, failures)
    print("匹配附件：%d；有效估值日：%d；产品：%d" % (len(found), len(snapshots), len(products)))
    print("独立报告：%s" % path)
    if failures:
        print("邮箱失败：%s" % "；".join(failures))
    if errors:
        print("解析提示：%d项" % len(errors))


if __name__ == "__main__":
    main()
