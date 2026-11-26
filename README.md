# 导热片寄售履约与阶梯差价

寄售仓和出账对手交换片件、跨厂去向、现场实耗和封账明细。

《寄售用量与账单》说明在 [交换约定](docs/exchange.md)。`fixtures/exchange.json` 是本机联调使用的合成记录。`consignment.py` 基于这些资料完成片级代管核对、实耗裁决、阶梯计价和账单调节；`service.py` 把处理结果通过回环地址暴露给采购、库管和出账系统。

运行环境为 Python 3.11.2，只依赖标准库。执行 `python3 -m unittest discover -s tests -v` 检查交换资料与履约规则。

- `python3 partner.py`：回放对接方已经留存的回执，`GET /health` 返回服务状态，`GET /v1/receipts?at=...` 按到达时点查询。如需模拟查询暂时失败，启动时传入 `--unavailable`。
- `python3 service.py`：履约查询服务（默认 8088 端口）。`GET /v1/warehouse`、`/v1/procurement`、`/v1/invoices`、`/v1/claims`、`/v1/scope` 分别对应库管视图、采购逐片对账、封账与欠款、认领入口和下一账期差额；`POST /v1/recovered-usage` 接收生产恢复后补传的用量。
