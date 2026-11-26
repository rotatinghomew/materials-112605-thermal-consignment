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
