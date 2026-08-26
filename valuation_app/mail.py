import email
import hashlib
import imaplib
import os
import re
import ssl
from datetime import datetime
from email.header import decode_header

from .config import PRODUCTS, match_product


VALUATION_MARKERS = ("估值表", "估值报表", "资产估值", "证券投资基金估值", "四级科目", "净值报告")
EXCEL_EXTENSIONS = (".xls", ".xlsx")


def load_env(path=".env"):
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key.strip()] = value.strip().strip("'\"")
    return values


def accounts(env_path=".env"):
    env = load_env(env_path)
    result = []
    for suffix in [""] + [str(i) for i in range(2, 10)]:
        user = os.environ.get("IMAP%s_USER" % suffix, env.get("IMAP%s_USER" % suffix, ""))
        password = os.environ.get("IMAP%s_PASS" % suffix, env.get("IMAP%s_PASS" % suffix, ""))
        if user and password:
            result.append({
                "server": os.environ.get("IMAP%s_SERVER" % suffix, env.get("IMAP%s_SERVER" % suffix, "mail.gyzq.com.cn")),
                "port": int(os.environ.get("IMAP%s_PORT" % suffix, env.get("IMAP%s_PORT" % suffix, "993"))),
                "user": user, "password": password,
            })
    return result


def _decode(value):
    result = []
    for part, charset in decode_header(value or ""):
        if isinstance(part, bytes):
            decoded = None
            for encoding in (charset, "utf-8", "gb18030"):
                if not encoding:
                    continue
                try:
                    decoded = part.decode(encoding)
                    break
                except (LookupError, UnicodeDecodeError):
                    pass
            result.append(decoded if decoded is not None else part.decode("utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def _safe(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "估值表.xls"


def _connect(account):
    try:
        client = imaplib.IMAP4_SSL(account["server"], account["port"], ssl_context=ssl.create_default_context())
        return client, False
    except ssl.SSLCertVerificationError:
        # 内部邮件服务器证书链当前不完整；仅在严格校验明确失败时回退。
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        client = imaplib.IMAP4_SSL(account["server"], account["port"], ssl_context=context)
        return client, True


def _existing_hashes(output_dir):
    hashes = set()
    for base, _, files in os.walk(output_dir):
        for filename in files:
            if not filename.lower().endswith(EXCEL_EXTENSIONS):
                continue
            try:
                with open(os.path.join(base, filename), "rb") as handle:
                    hashes.add(hashlib.sha256(handle.read()).hexdigest())
            except OSError:
                pass
    return hashes


def match_valuation_attachment(subject, filename):
    searchable = "%s %s" % (subject, filename)
    product = match_product(searchable)
    if not product or not any(marker in searchable for marker in VALUATION_MARKERS):
        return None
    return product


def download_valuations(output_dir="products", env_path=".env", latest_only=False, account_users=None):
    configured = accounts(env_path)
    if account_users:
        allowed = set(account_users)
        configured = [account for account in configured if account["user"] in allowed]
    if not configured:
        raise RuntimeError("未找到可用邮箱配置，请检查 .env 和账号筛选条件")
    os.makedirs(output_dir, exist_ok=True)
    hashes = _existing_hashes(output_dir)
    saved, duplicates, failures, account_results = [], 0, [], []

    for account in configured:
        client = None
        try:
            client, insecure_tls = _connect(account)
            client.login(account["user"], account["password"])
            status, _ = client.select("INBOX")
            if status != "OK":
                raise RuntimeError("无法选择收件箱")
            status, data = client.search(None, "ALL")
            if status != "OK" or not data:
                raise RuntimeError("邮箱搜索失败")
            message_ids = data[0].split()
            if latest_only:
                message_ids = message_ids[-2000:]
            matched = 0
            for position, message_id in enumerate(reversed(message_ids), 1):
                status, payload = client.fetch(message_id, "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                message = email.message_from_bytes(payload[0][1])
                subject = _decode(message.get("Subject"))
                for part in message.walk():
                    raw_name = part.get_filename()
                    if not raw_name:
                        continue
                    filename = _decode(raw_name)
                    if not filename.lower().endswith(EXCEL_EXTENSIONS):
                        continue
                    product = match_valuation_attachment(subject, filename)
                    if not product:
                        continue
                    content = part.get_payload(decode=True)
                    if not content:
                        continue
                    matched += 1
                    digest = hashlib.sha256(content).hexdigest()
                    if digest in hashes:
                        duplicates += 1
                        continue
                    raw_date = message.get("Date")
                    try:
                        mail_date = email.utils.parsedate_to_datetime(raw_date) if raw_date else datetime.now()
                    except (TypeError, ValueError):
                        mail_date = datetime.now()
                    product_dir = os.path.join(output_dir, product)
                    os.makedirs(product_dir, exist_ok=True)
                    target = os.path.join(product_dir, _safe(mail_date.strftime("%Y%m%d") + "_" + filename))
                    if os.path.exists(target):
                        stem, ext = os.path.splitext(target)
                        target = stem + "_" + digest[:8] + ext
                    with open(target, "wb") as handle:
                        handle.write(content)
                    hashes.add(digest)
                    saved.append(os.path.abspath(target))
                if position % 250 == 0:
                    print("%s: 已检查 %d/%d 封邮件，匹配附件 %d" %
                          (account["user"], position, len(message_ids), matched))
            account_results.append({"user": account["user"], "checked_messages": len(message_ids),
                                    "matched_attachments": matched, "insecure_tls_fallback": insecure_tls})
        except Exception as exc:
            failures.append("%s: %s: %s" % (account["user"], type(exc).__name__, exc))
        finally:
            if client:
                try:
                    client.logout()
                except Exception:
                    pass
    return {"downloaded": saved, "duplicates": duplicates, "failures": failures,
            "accounts": account_results, "configured_products": len(PRODUCTS)}
