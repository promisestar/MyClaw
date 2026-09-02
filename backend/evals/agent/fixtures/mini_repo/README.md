# Mini fixture for MyClaw Agent eval

This is a tiny workspace used by L2 scenarios (`craft_edit_file`, `ask_code_explain`, etc.).

Copy this directory to a temp path and authorize it via the workspace API before running evals.

## 目录说明

| 路径 | 用途 |
|------|------|
| `sample_app.py` | 入口脚本，打印 VERSION |
| `src/utils.py` | 价格计算（含一个待修的折扣 bug） |
| `config/app_config.json` | 运行参数 |
| `data/sales.csv` | 销售流水，供统计场景使用 |
| `docs/architecture.md` | 架构说明 |
| `notes/*.md` | 会议与设计记录，供 RAG / 总结场景使用 |
