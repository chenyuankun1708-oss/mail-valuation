# strategy_lab

独立、只读行情、研究用途的指数ETF配置包。它只消费项目已经落盘的固定白名单行情缓存，运行数据写入Git忽略的`strategy_lab_data`，并通过版本化`result.json`交给主网页读取。模块不依赖网页JavaScript或全局变量，不连接券商，不生成订单。

运行：`python -m strategy_lab.cli run`。流水线依次固化数据来源哈希、清洗行情、生成时点因子、运行扩展窗口Ridge与规则评分基线、执行月度走步回测，并用最近已结束月份重新拟合最新建议权重和特征贡献。未结束的当月不会被当作月末信号。

数据边界：宽基、风格和申万一级行业代码来自`config.py`固定清单；输入仅为`market_data/index_daily.json`和`market_data/market_research.json`。宏观只使用`info_date`不晚于信号日的数据。若历史ETF份额×NAV不足120个当时可得交易日，结果明确标记为指数研究信号，不用当前ETF回填历史。
