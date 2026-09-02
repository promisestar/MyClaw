# Mini Repo 架构说明（评测夹具）

## 模块划分

- `sample_app.py`：入口脚本，`main()` 打印 `VERSION`。
- `src/utils.py`：价格计算，含 `TAX_RATE`、`calc_discount`、`format_price`、`total_with_tax`。
- `config/app_config.json`：运行参数（`debug`、`max_retries`、`features`）。
- `data/sales.csv`：销售流水，列为 `date,region,product,quantity,price`。
- `notes/`：会议与设计记录。

## 约定

1. 所有对外函数都要有中文 docstring。
2. 金额计算统一走 `src/utils.py`，不要在调用方重复实现。
3. 配置只从 `config/app_config.json` 读取，禁止硬编码。
4. 任何破坏性操作（删除文件、执行外部命令）都需要用户显式确认。
