# 寄售用量与账单

数量按单件计，价格为分。期间以工厂当地月份确定。现场顺序字段 seq 在同一合同同一业务日内从小到大且唯一。tiers 每对是累计起点及单价。retro 指当期全量折扣，marginal 指超出档位量折扣。退回未消耗计库存恢复。credit_return 需原消耗确认，按合同抵减原件所在账期且有独立退货业务凭据。

资料使用 UTF-8 JSON。每个集合各自维护记录 id，跨集合引用使用相应记录 id。事件中的 happened 是业务发生时间，arrived 是对接方可提供该回执的时间。到达时间不能代替业务时间。这些记录来自不同流程阶段，读到一个回执并不代表本地已做过相应操作。

## `contracts`

价格体例、买方和共同计量站点。字段为 `id`, `buyer`, `sites`, `mode`, `tiers`, `return_credit`。

## `pieces`

初始寄售实物，预定合同和所在工厂。字段为 `id`, `contract`, `site`, `package`。

## `orders`

采购委托与转厂许可。字段为 `id`, `contract`, `intended_site`, `fallback`, `pieces`, `ship`。

## `movements`

发出/接手需分别提供，工厂可补传原票。字段为 `id`, `piece`, `sender`, `receiver`, `state`, `happened`, `arrived`。

## `usage`

工厂确认实耗，日期和来源保留原样。字段为 `id`, `piece`, `site`, `seq`, `occurred`, `reported`。

## `returns`

退回及批准抵扣记录，关联实际消耗。字段为 `id`, `piece`, `type`, `usage`, `occurred`, `reported`。

## `receipts`

出账系统回执。账单的明细来源在 usages。字段为 `id`, `invoice`, `period`, `usages`, `phase`, `happened`, `arrived`。

## 回执查询

本机回放只提供对接方的既有回执。`GET /v1/receipts?at=...` 的 `at` 是 URL 编码后的带偏移时间，返回对象中 `receipts` 是在该时间之前及当时可取得的回执。同一条数据可以在连续查询中出现，列表先后不代表业务先后。`503` 表示当前无法查询回执。

指定 `--journal` 时，`POST /v1/dispatches` 接收本地联调请求并登记原文。接收只代表到达对手方，不会生成履约回执。指定 `--drop-reply` 可在接收后断开。请求正文由业务客户端提供，回执仍由交换资料独立给出。

## 履约处理

`consignment.py` 按上述资料做片级处理，`service.py` 在回环地址 8088 暴露只读结果，查询可带 `at` 参数按到达/回报时点回放。处理规则与《寄售用量与账单》一致：

- 代管台账按 `movements` 的业务时间回放 `dispatched`/`accepted`/`reported_lost`，给出每片当前代管工厂、在途/遗失状态和开封状态；`arrived` 不改变业务时点的归属。
- 实耗裁决只承认合法经手该片的工厂：在途、遗失期间或工厂无代管权的确认不可结算；转厂接手必须落在采购委托的 `intended_site`/`fallback` 内。同一片只能有一条可结算实耗，重复确认挂起。
- 越权或遗失期间的实耗不自动出账，转成认领（claim）并保留实耗、移动票和委托证据；对应片不得重新列为可供领用。
- 计价按合同把两个工厂的确认净用量归集到同一账期，同日按 `seq` 现场顺序计价，迟报实耗按 `occurred` 的业务月份归账。`marginal` 仅对超出档位的量用新价；`retro` 按全期净用量落档并对净存量全量追补。`credit_return` 仅在合同允许时抵减原实耗所在账期，`unused` 只恢复库存、不产生金额。
- 账单调节保留已受理回执的原应付；后续差额标注为 `late_report`、`credit_return` 或 `re_tier`，并归到合同的 `buyer`。受理状态无法确认的明细进入 blocked（`acceptance_unknown`），不得把原应付重发到下一账期，只返回仍可确定的账单与明细范围。

### `GET /v1/warehouse`

库管视图：每片的代管工厂、状态、开封、可否退回/转厂、是否可供领用。被拒绝或挂认领的片不会出现在可供领用清单。

### `GET /v1/procurement`

采购视图：逐片对齐实耗裁决、退回类型、账单号、原账单金额、抵扣金额和认领号；封账记录与当前欠款在 `/v1/invoices` 同时可查。

### `GET /v1/invoices`

已受理/已结清账单、原应付金额、未付余额与按买方汇总的欠款。

### `GET /v1/claims`

暂停自动出账的认领入口及证据链。

### `GET /v1/scope`

下一账期差额报告：可确定差额、blocked 明细、可确定账单/实耗范围和各买方下一张应付单的变动额。

### `POST /v1/recovered-usage`

生产恢复后上传用量，正文为 `{"at": <带时区时间>, "usages": [...]}`，走与现场记录相同的裁决与计价处理，返回这些用量是否改变采购方下一张应付单；违反代管规则的上传只生成认领、不产生应付。

