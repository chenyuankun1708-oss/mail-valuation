# Result schema v1

`strategy_lab_data/result.json`包含：`schema_version`、生成时间、研究状态、模型与基线版本、基准、调仓规则、最新信号日、`latest_model`、建议权重、走步回测、`validation`、`model_comparison`、数据血缘和免责声明。

建议权重逐项保存指数代码、名称、资产层级、权重、预测值、特征贡献、当时可得ETF和执行状态。回测逐期保存训练区间、训练样本数、模型/基线权重、换手、约束检查和收益；不得加入券商订单或交易凭据字段。

`latest_model`保存最新已结束月份的训练区间、样本数、Ridge系数、预测、权重及约束结果；`validation`保存无前视、样本外期数、组合约束、成本场景和缺失特征状态；`model_comparison`保存10bp成本下Ridge与规则基线的年化收益差异。
