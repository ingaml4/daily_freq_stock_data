# 独立仓位管理研究 v1

本功能是纯试算与单批候选研究。不会下单、发送消息、改实盘提醒或改变既有 A/B/C、O/R 研究入口。固定研究本金 240000 元不代表券商净值、现金或真实可买数量。没有参数优越性结论，不自动晋级实盘。

## 研究假设

| 约束 | v1 候选参数 | 24 万元基准 |
| --- | --- | --- |
| 分批首仓计划风险 | 0.25% | 600 元 |
| 同一股票合计计划风险 | 0.50% | 1200 元 |
| 单票市值 | 20% | 48000 元 |
| 每个相关题材市值 | 30% | 72000 元 |
| 总市值 | 55% | 132000 元 |
| 组合合计计划风险 | 1.25% | 3000 元 |
| 单票压力损失 | 1% | 2400 元 |
| 压力价格 | 入场下跌 6% 与结构失效价取较低者 | 情景假设 |
| 月内回撤响应 | 5% 后新风险预算减半，10% 后不新开 | 研究响应 |

按 100 股向下取整，并取可用现金、已核验卖一数量、单票额度、每个题材额度、组合额度、计划风险和压力损失容许股数的最小值。股数求解逐笔计入双边最低佣金、过户费、卖出税费与滑点，不把最低佣金当成固定每股常数。费用沿用 `execution.settings`，可传实际费率。零股代表已知约束下不足一手，`null` 代表资料不完整。

计划损失是按假定失效价退出的试算，不能保证成交。压力测试同样不是损失上限。T+1 与跌停、停牌可能使风险超出这些数字。组合同时受冲击时的合计压力损失单独输出，v1 尚未为它另设上限。分散持仓仍可能受同一风险影响，参见 [Investor.gov 的分散投资说明](https://www.investor.gov/introduction-investing/getting-started/asset-allocation)。交易制度沿用项目已核验的 [2026 年交易规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml) 与 `execution`，科创板按本人权限排除。

## 账户与委托契约

`size_order(candidate, account, mode=..., policy=Policy(), costs=...)` 是可导入入口。

账户须包含带时区的 `as_of`、`kind`（`research` 或调用方已经核验的 `broker_verified`）、`equity`、`monthly_peak_equity`、`cash_available`、`cash_basis`、`positions` 和 `pending_orders`。月内高水位必须经资金划转调整，缺失时拒绝试算。截图或口述的约数不能标成 `broker_verified`。

`cash_basis=before_pending` 会减掉列出的在途买单预留现金。`net_of_pending` 表示券商可用现金已经扣除这些占用，不重复扣除。两种口径都将剩余委托数量计入单票、题材与风险暴露。缺委托列表不能当成空列表。

每条持仓与未成交委托余量需要唯一 `id`、`secid`、`themes`、`market_value`、`planned_loss` 和 `stress_loss`。在途委托另需 `reserved_cash`，应包含费用。调用方先按券商委托号和成交号核对部分成交，已成交数量进入持仓、剩余数量进入在途，不能把原始全额委托重复加入。每笔市值和风险在组合只计一次，跨题材的全部市值进入每一个相关题材的额度。

候选需要 `secid`、`entry_price`、`risk_price`、`at`、`risk_available_at`、`themes`、`themes_available_at`、已核验的 `ask_shares` 和 `execution_verified=true`。这个布尔字段是上游成交校验结果，试算器不会自行访问券商或证明成交。正式研究由原分钟引擎校验新鲜度、价格制度、完整分钟、风险路径和报价覆盖后传入。账户快照默认最多早于候选 60 秒，风险及题材证据不得来自未来。

`risk_add` 另需 `previous_entry_at`、`previous_entry_price`、`previous_risk_price`、`previous_signal_id`、`add_signal_available_at` 和 `add_signal_id`。后续证据必须在首仓之后、下单报价之前，并有不同事件 ID。沿用同一结构风险位，不允许以跌价补仓冒充确认加仓。现有仓位及在途占用共享该票总额度。

本地文件入口如下，输入文件属于私人账户资料，不应提交到仓库。

```sh
python -m emscan.position_sizing --request /private/request.json --out /private/sizing.json
```

请求对象为 `{"candidate": {...}, "account": {...}, "mode": "risk_first"}`，可选 `policy`、`costs`。返回每项上限对应股数、实际限制项、剩余现金、计划损失和跳空压力损失。

## 同批比较

```sh
python -m emscan.position_report \
  --db data/market.sqlite \
  --archive /private/capture/source-opportunities.json \
  --archive-sha256 <实际归档摘要> \
  --capture-config /private/capture/candidate-config.json \
  --events /private/capture/events.jsonl \
  --as-of <实际已到的北京时间含时区> \
  --capital 240000 \
  --out /private/position-research/report.json
```

数据库只读事务，同一机会归档、配置与事件文件均保留摘要。输入事件须是稳定快照，不能截断或替换采集器正在写的文件。逐候选按原信号风险、可见时序和 O/R 条件作独立一手探测，原 O/R 报告仍按每单 10 万元运行。仓位研究的分母与原 10 万元分母分别列示，不能混用。

每个 O/R 组分别比较四个参数组。

* `all_in` 尽量使用剩余现金，仍受当笔可见卖一数量限制。
* `fixed` 每票现金预算 48000 元，仍受剩余现金和卖一数量限制。
* `risk` 一次按同票总风险与所有组合约束分配。
* `risk_first` 只配置分批首仓。没有后续独立加仓证据时加仓数为零，标出尚缺的证据。

各组取同一个最早满足一手条件的报价，在此报价按各自约束分配。不会给大仓组跳到后来的报价，也不把涨停排队当成成交。相同时刻按代码排序，同一证券重复模式优先 A、B、C 并披露去重。研究 v1 限单信号日批次，开仓预算固定使用期初净值与入场成本暴露，不能混称为跨日滚动、盘中按市价调整的风险组合。加仓完整绩效与跨批次复利另需事件流支持。

固定金额可用 `--fixed-amount 24000` 单独覆盖，仅改变 `fixed` 组预算。默认仍为 48000 元。输出顶层 `fixed_amount` 与 `policy.fixed_amount` 保存实际金额，`fixed_amount_basis` 明确包含买入费用的每笔现金预算。不同预算使用不同输出路径，例如 `report-fixed-24000.json`，不要覆盖已交付的默认报告。11 月底本人点名候选试验另有冻结样本与每笔 24000 元口径，此处回放的 11 月 13 日 A/B/C 归档不能计入该前瞻试验。输出 `cohort_basis` 明示归档来源，不会自动纳入点名样本。

退出在重新计算股数后调用原 `close_exits`，相同 D+5、T+1、停牌、涨跌停、成交量、费用和权益变动校验。没有完整日线或无法核验的持仓继续占位，收益与回撤会标缺失。输出费后收益、已完成每笔 R、最大回撤、观察到的最差隔夜缺口、连续亏损、放弃率、资金利用率、证券 HHI、题材权重和逐笔限制原因。相同退出日的连续亏损按原入场顺序和代码处理，不能解释成独立时序试验。

主线使用冻结标签，没有主线时仅以冻结申万二级作为行业集中度代理。SW2 不能覆盖航天、机器人等全部交叉题材，也不是收益相关系数。`estimated_return_correlation=null` 明示尚未估计。未到 D+5 或有入口/退出缺口时不生成成熟收益结论，单批结果不用于挑最优参数。

## 已查明的数据缺口

当前实际成交记录能区分本人报告成交、截图与未知字段，但没有完整券商委托生命周期、当日净值、实际可用现金、费用、资金划转和月内净值高水位。部分持仓市值仍来自旧日。研究程序不把这些未知字段补成零。

2026-11-13 的第一批应另列开盘覆盖和 09:25 撮合缺口，观察到卖一或两根分钟不等于满足全部严格成交条件。完整 O/R 成交审核由原分钟报告输出，仓位研究不能放宽这一约束来制造收益样本。

验证入口为 `pytest tests/test_position_sizing.py tests/test_position_report.py tests/test_entry_minute_study.py`。涵盖在途现金两种口径、题材重叠、最低佣金、整手、压力风险、月内回撤、未来证据、数据缺失、一手探测、现金守恒、共同日期退出和原 O/R 默认行为。
