# 独立仓库迁移清单

1. 使用`git filter-repo --path strategy_lab --path-rename strategy_lab/:`保留模块历史。
2. 将`strategy_lab_data`继续作为仓库外运行目录，仅迁移需要的缓存副本。
3. 保持`config.py`白名单、`SCHEMA.md`和CLI兼容；主项目只读取发布后的`result.json`。
4. 将当前项目行情缓存适配器替换为独立仓库的数据下载器时，保持原始快照、可用时间、错误状态和Schema不变。
5. 独立仓库上线前重跑无前视、缺失数据、成本、换手、约束和结果复现测试。
