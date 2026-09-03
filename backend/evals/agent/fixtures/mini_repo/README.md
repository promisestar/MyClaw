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
| `data/report.xlsx` | 销售报表（多模态 xlsx 场景） |
| `data/large_log.txt` | 大日志文件（触发 ContextGuard 截断场景） |
| `docs/architecture.md` | 架构说明 |
| `docs/spec.pdf` | PDF 文档（多模态 pdf 场景） |
| `assets/chart.png` | 图表图片（多模态 image 场景） |
| `notes/*.md` | 会议与设计记录，供 RAG / 总结场景使用 |
| `tests/test_utils.py` | 含一个故意写错的失败测试，供调试场景使用 |
