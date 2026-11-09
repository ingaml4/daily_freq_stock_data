# 自选出池复核 API

`GET /api/watchlist-risk?date=2026-11-06&symbols=SH600519,SZ000001,300001.SZ`

沿用网站的登录 Cookie 或 HTTP Basic。每次请求 1–100 个代码，用英文逗号分隔，支持 `SH600519`、`600519.SH` 和内部代码 `1.600519`。返回结果按输入顺序逐行排列，保留重复项。日期为东八区交易日 `YYYY-MM-DD`。参数不合法返回 400，未登录返回 401。单个代码无效、未知或市场暂不支持时，该行返回缺数状态。

## 响应

根对象包含 `requested_date`、`latest_market_date`、`recent_trade_dates`、`rule_version`、`parameters`、`data_source`、`configured_kline_adjust`、`max_symbols` 和 `rows`。记录如下。

| 行字段 | 含义 |
|---|---|
| `requested_symbol`、`symbol`、`secid`、`name`、`board` | 输入和本地股票标识 |
| `requested_date`、`data_date` | 请求日与截至请求日最近一根有效日线日期。两者可能不同 |
| `bar` | 请求日有效的 OHLC、成交量、成交额、数据库来源；缺失时为 null |
| `stock_return_pct`、`relative_pp` | 请求日个股涨跌幅（%），及减去所属市场指数涨跌幅后的百分点差 |
| `benchmarks.market`、`benchmarks.shanghai` | 所属市场与统一上证指数对照，分别返回代码、名称、日期、收盘、来源、百分数涨跌、个股涨跌、百分点差与三项比较信号 |
| `benchmark` | 所属市场基准，与 `benchmarks.market` 相同 |
| `signals` | 当日七项信号。三个市场比较信号采用所属市场基准；与上证的比较见 `benchmarks.shanghai.signals` |
| `recent` | 截至请求日最近五个本地市场交易日的逐日 `{date,signals,benchmarks}`，按日期升序 |
| `reason` | 整行缺数原因；单项不可用时查该信号的 `reason` |

所有信号都有 `status`（`triggered`、`not_triggered`、`unavailable`）、`reason`、`trigger_date`、`confirmation_date`、`new_trigger` 和 `evidence`。判定使用截至各自观察日收盘的数据。当前条件连续满足时，`new_trigger` 只在由不满足变为满足时为 true；前日状态未知时为 null。单日穿越与确认峰按事件判定。无成交、未发布和无对应交易日均保留当日不可用状态，`recent` 中历史事件只代表其各自日期。

例如，假设股票当日跌 1%，所属市场指数涨 1%，`relative_pp` 为 -2，`market_up_stock_down`、`market_up_stock_down_significant` 和 `relative_underperformance` 均触发。若指数数据缺失，这三项标为 `unavailable`，其他价格和指标信号仍可使用。

## 规则 2026-11-09.1

| 信号键 | 日线口径 |
|---|---|
| `high_volume_drop` | 当日跌幅至少 3%，且成交量为前 5 个市场交易日均量的至少 1.5 倍，要求 5 日均有有效成交 |
| `persistent_decline` | 最近 5 个市场交易日至少 4 日收跌，5 日个股日收益复合累计 ≤ -5% |
| `macd_dead_cross` | 前一交易日 DIF ≥ DEA，当日 DIF < DEA。两日分别需至少 100 根有效日线预热 |
| `macd_bearish_divergence` | 价格峰的最高价须严格高于左右各 2 个市场交易日的最高价。在峰间距 5–60 个市场交易日中选择最近已确认前峰，忽略间距过短者。后峰更高且后峰日 DIF 更低即成为候选，`trigger_date` 是后峰日，`confirmation_date` 是右侧第 2 个交易日。证据含两峰日期、最高价、DIF 和峰间距。两个峰分别需至少 100 根预热日线 |
| `market_up_stock_down` | 对应指数涨幅 > 0%，同时个股涨幅 < 0% |
| `market_up_stock_down_significant` | 同时满足方向相反和个股相对该基准落后 ≥ 2 个百分点 |
| `relative_underperformance` | 个股涨跌幅减基准涨跌幅 ≤ -2 个百分点，两者方向可以相同 |

所属市场按沪主板上证综指 000001、深主板深证成指 399001、创业板创业板指 399006、科创板科创50 000688 映射。每行还提供统一上证综指。行业归属库缺少历史成分快照，此接口提供指数层面的相对表现。

股票行情直接读取 `bars`，日收益优先级固定为库中的 `pct_chg`（%）。成交量和成交额沿用来源口径，量比取同股相同字段。快照使用当时入库价格，历史 K 线使用采集时的配置（默认前复权）。`configured_kline_adjust` 表示当前配置，逐日来源见 `bar.source`；历史按当前本地库重算，无法重建某天之后被供应商修订前的价格版本。指数收益取对应日期 `benchmark_bars.pct_chg`。

MACD 使用项目公式 EMA12、EMA26、EMA9。两端同源且指标完整时优先使用已存 `indicators`；缺少端点时，使用同一段截至观察窗口的本地有效日线重算两个端点，证据注明 `recomputed_from_local_bars`。窗口内预热不足、缺少上一交易日行情、缺少峰附近应有的行情或缺少同日指数收益时分别返回对应的缺数原因。仅知道缺少有效日线时，原因统一为 `no_valid_bar`，其中包括停牌和上游尚未采集。
