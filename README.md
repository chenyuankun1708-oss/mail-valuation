# 邮件估值表收益看板

公网小范围分享、密码设置、Cloudflare Quick Tunnel和Windows计划任务的配置方法见
[SHARING.md](SHARING.md)。

本项目只做一件事：从邮箱下载指定 17 只单一资产管理计划的估值表，读取资产净值、总份额、单位净值和累计单位净值，估算申购/赎回/分红，计算单一投资人的收益与收益率，并通过本地网页展示。

## 能否只靠估值表判断现金流

可以识别和估算，但不能保证每一种情况都能精确分类。

- 追加申购、部分赎回：相邻估值表的总份额发生变化时，可以高置信度判断方向；金额按“份额变化 × 当日单位净值”估算。若两个估值日之间发生多笔反向交易，只能看到净变化。
- 分红：累计单位净值与单位净值之间的差额扩大时，可以识别为分红候选并估算金额。若模板没有正确维护累计净值，或分红与赎回同时发生，仍需分红公告或银行流水确认。
- 收益金额：17只产品均只有一个客户，因此直接按客户持有的全部资产净值计算：`期末资产净值 - 期初资产净值 - 申购金额 + 赎回金额 + 现金分红`。
- 年化收益率：同时展示资金加权XIRR和累计单位净值CAGR。XIRR使用期初资产、台账申赎、现金分红和期末资产；净值年化收益率只使用所选区间内的真实累计单位净值。
- 风险指标：累计单位净值相邻变化按实际间隔天数换算为日对数收益，据此计算年化波动率和固定2%无风险利率的夏普率；最大回撤使用累计单位净值历史峰值到谷值的最大跌幅。
- 总份额：只用于核验申赎和估值表一致性，不用于计算收益。部分模板无法可靠提取真实份额，程序可能用 `资产净值 ÷ 单位净值` 反推；反推份额会随净值波动，不代表真实申赎。
- 边界注意：若台账登记日期与估值表确认申赎的日期不一致，按月统计可能发生跨期偏差，应核对边界附近流水和估值表。
- 新成立产品：若开始日期前没有估值表，但区间内已有首次投资和期末估值，按期初资产为0计算，区间内申购从收益中扣除。

建议至少保留每个交易日或每次现金流前后的估值表。页面会展示事件依据、置信度和超过 45 天的数据间隔警告。

## 安装与配置

需要 Python 3.7+：

```powershell
python -m pip install -r requirements.txt
```

在项目根目录创建 `.env`（不要提交密码）：

```dotenv
IMAP_SERVER=mail.example.com
IMAP_PORT=993
IMAP_USER=user@example.com
IMAP_PASS=your-password

# 可选的第二个邮箱，后续可继续使用 IMAP3_* ... IMAP9_*
IMAP2_SERVER=mail.example.com
IMAP2_PORT=993
IMAP2_USER=user2@example.com
IMAP2_PASS=your-password

# Wind Oracle指数行情（真实密码只放在本机.env）
WIND_DB_USER=your-wind-user
WIND_DB_PASSWORD=your-wind-password
WIND_DB_DSN=your-oracle-service-name
```

## 使用

把手动放入 `products/`（包括 `products/FOF/`）的估值表复制归档到标准产品目录。原始文件保留不动，附件内容按 SHA-256 去重：

```powershell
python app.py organize
```

生成可直接打开、无需启动服务的任意区间收益计算页面：

```powershell
python app.py build
```

单独更新中证1000、中证500和沪深300的Wind日线缓存：

```powershell
python app.py benchmark
```

行情来自本机配置的Wind Oracle数据库，从 `AIndexEODPrices` 读取三只指数日收盘，不再访问东方财富或其他公网行情接口。凭据只从环境变量或被Git忽略的 `.env` 读取；缓存保存于 `market_data/index_daily.json`。`build` 只读取本地缓存、不连接数据库；单只指数查询失败时保留该指数上次成功数据并在网页提示，数据库连接失败时完整保留旧缓存，完全没有缓存时原有收益功能仍可使用。`refresh` 的顺序为邮件下载、估值表整理、指数更新、网页生成。

生成结果位于项目根目录的 `index.html`。页面可人工选择开始和结束日期，并在“项目说明”中集中展示计算口径、常用代码指令和历史改动。如果边界当天没有估值表，每只产品分别使用该日期之前最近的估值表。申购赎回明细读取根目录《专户资金台账.xlsx》的 `2024`、`2025`、`2026` 工作表；收益金额按期初/期末资产净值扣除台账净申购并加回现金分红计算。产品详情同时展示资金加权XIRR、累计净值风险指标、指数同期表现、相对中证1000的Alpha/Beta和归一化对比曲线。对比图默认勾选当前产品和三只指数，其他顶层产品默认不选，可按需勾选进行同期比较；不再重复展示单独的产品净值曲线。能解析底层持仓的FOF还展示底层产品收益率贡献柱状图，贡献率按底层期间估算收益除以FOF区间首个真实估值日资产净值计算。当前总投资额读取 `专户资金余额(2026)`，会议批准额度汇总2026流水区的审核金额，不读取下方“额度/占用”汇总区。计算口径和已知限制详见 [CALCULATION_METHOD.md](CALCULATION_METHOD.md)。

下载所有匹配且尚未保存的估值表：

```powershell
python app.py download
```

邮箱很大时只检查最近 2000 封：

```powershell
python app.py download --latest-only
```

启动网页（默认会打开 `http://127.0.0.1:8000`）：

```powershell
python app.py run
```

仅输出 JSON 分析结果：

```powershell
python app.py analyze
```

下载最新邮件、整理估值表并重新生成网页：

```powershell
python app.py refresh
```

启动仅监听本机回环地址、使用 `.env` 用户名和密码保护的分享服务：

```powershell
python app.py share
```

## 数据要求与目录

估值表支持 `.xls` 和 `.xlsx`，可位于 `products/` 的任意子目录。系统仅接受配置中的 17 只产品，并按表头或文件名匹配。至少需要两张不同估值日的表才能计算收益。

核心代码：

- `valuation_app/config.py`：唯一的产品白名单与别名。
- `valuation_app/mail.py`：IMAP 附件下载与内容去重。
- `valuation_app/parser.py`：多模板估值表字段解析。
- `valuation_app/analytics.py`：事件识别、收益和收益率。
- `valuation_app/benchmark.py`：三只Wind指数的Oracle查询、本地缓存和故障回退。
- `valuation_app/web.py`：零前端依赖的本地网页。

## 测试

```powershell
python -m unittest discover -s tests -v
```

估值表解析和事件判断是辅助核算工具，不替代管理人对账单、交易确认书或托管银行流水。
