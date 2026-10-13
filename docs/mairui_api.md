# 麦蕊智数接口速查（本项目用到的 6 类）

数据源文档：<https://www.mairui.club/hsdata>（沪深A股数据）。接口域名 `https://api.mairuiapi.com`
（同一服务的备用域名 `https://api.mairui.club`），**全部需要证书（licence）**，证书免费领取、
按档位限频。下面是本项目实际用到、并已用真实响应核对过字段的接口。

| 用途 | 接口 | 本项目里的位置 |
| --- | --- | --- |
| 股票列表（代码 + 名称） | `GET /hslt/list/{licence}` | `mairui.fetch_universe()` |
| 当日全市场行情快照 | `GET /hsrl/real/all/{licence}` | `mairui.fetch_spot()` 方式 `all` |
| 当日多股行情（≤20 只） | `GET /hsrl/ssjy_more/{licence}?stock_codes=a,b,…` | 方式 `more` |
| 当日单股行情 | `GET /hsrl/ssjy/{code}/{licence}` | 方式 `single` |
| 历史日线 | `GET /hsstock/history/{code}.{MKT}/d/{adjust}/{licence}` | `mairui.fetch_kline_many()` |
| 日频指标 MACD/KDJ/BOLL/MA | `GET .../hsstock/history/{kind}/{code}.{MKT}/d/{adjust}/{licence}` | `mairui.fetch_indicators_many()` |
| 交易日历 | `GET /tcalendar/list/{year}/{licence}` | `mairui.fetch_trade_calendar()` |
| 股票基础信息（流通股本/上市日期） | `GET /hsstock/instrument/{code}.{MKT}/{licence}` | `mairui.fetch_instrument_many()` |

> 所有解析逻辑都是纯函数（`emscan/mairui.py`），并配了用**真实报文**写的单测
> （`tests/test_mairui_parse.py`），所以没有网络也能回归。
> 部署后第一步建议跑 `python3 -m emscan check --probe`，它会打印各接口的真实样本。

## 0. 证书与限频（先看这个）

* 证书形如 `LICENCE-XXXX-XXXX-XXXX-XXXX`，放在 `config.yaml` 的 `mairui.licence`，
  或环境变量 `MAIRUI_LICENCE`（推荐，避免进 git）。
* 限频按档位：**基础 300 次/分钟**、包月/体验 1000、包年 3000、钻石 6000。
  客户端用 `mairui.rate_limit_per_min` 做全局节流（跨线程共享最小间隔），填对档位才不会浪费配额
  或被平台拦截。文档标注的"全市场"类接口（`hsrl/real/all`、`hsrl/ssjy/all`）开放范围是
  **包年/钻石版，或已开通的 Quant Pro 增强包**，且**每分钟只能请求 1 次**。
* 真正决定耗时的是"每分钟次数"，不是并发数。本项目首次全量回补的请求数估算：

  | 内容 | 请求数 | 300 次/分钟 | 3000 次/分钟 |
  | --- | --- | --- | --- |
  | 股票列表 + 当日快照 | 2 | 秒级 | 秒级 |
  | 历史日线（约 5400 只 × 1） | ≈5400 | ≈18 分钟 | ≈2 分钟 |
  | MACD/KDJ/BOLL（5400 只 × 3） | ≈16200 | ≈54 分钟 | ≈6 分钟 |
  | 每日增量（3 种指标 × 5400 只 + 少量日线缺口） | ≈16200 | ≈54 分钟 | ≈6 分钟 |

  日更之所以也要 3×N 次请求，是因为指标接口**一只股票一种指标一次请求**（没有全市场批量版）。
  想省时间可以只开需要的指标（`indicators.kinds: [macd, kdj]`），或用 `--limit` /
  `sync.max_symbols_per_run` 分多次跑满（脚本会自动续传，缺口不会漏）。

## 1. 股票列表

```
GET https://api.mairuiapi.com/hslt/list/{licence}
```

返回 `[{dm, mc, jys}, …]`，`dm` 已带市场后缀、`jys` 是交易所：

```json
[{"dm": "000001.SZ", "mc": "平安银行", "jys": "SZ"},
 {"dm": "600519.SH", "mc": "贵州茅台", "jys": "SH"}]
```

* 覆盖**沪深 A 股（实测 5203 只）**；北交所（`920xxx`/`8xxxxx`/`4xxxxx`）**不在这里**，
  需要文档里另一个栏目"京市A股数据"，本项目默认的 `universe.boards` 也不含 `bse`。
* 名称里带 `ST`/`*ST`/`退` 的，本项目标记为风险股，扫描/信号里默认剔除（`universe.exclude_st`）。
* 实测 `jys` 返回大写 `SH`/`SZ`（文档示例里是小写），两种都做了兼容。

## 2. 当日行情快照（三种口径）

三种都用同一套解析（`parse_quote_payload`）。字段表（实测）：

| 字段 | 含义 | 单位 | 本项目列 |
| --- | --- | --- | --- |
| `dm` | 股票代码（全市场接口不带后缀） | | `secid` |
| `p` / `yc` | 最新价 / 昨收 | 元 | `close` / `prev_close` |
| `o` `h` `l` | 开 / 高 / 低 | 元 | `open`/`high`/`low` |
| `pc` / `ud` | **涨跌幅 / 涨跌额** | % / 元 | `pct_chg` / `chg` |
| `v` / `cje` | 成交量 / 成交额 | **手** / 元 | `volume` / `amount` |
| `zf` / `hs` / `lb` | 振幅 / 换手率 / 量比 | % / % / 倍 | `amplitude` / `turnover` / `volume_ratio` |
| `lt` / `sz` | 流通市值 / 总市值 | 元 | `float_mv` / `total_mv` |
| `t` | 更新时间 | 字符串 | `ts`（用来推交易日） |
| `pe` `sjl` `zs` `fm` `zdf60` `zdfnc` | 市盈率/市净率/涨速/5分钟涨跌/60日/年初至今 | | 未落库 |

* **全市场**：`GET /hsrl/real/all/{licence}`，一次拿全市场（实测 5919 条，**含北交所**，
  但**没有股票名称**）。每分钟限 1 次。
* **多股**：`GET /hsrl/ssjy_more/{licence}?stock_codes=000001,600519,…`，一次最多 20 只。
  文档字段表里换手率叫 `tr`、成交量叫 `tv`；实测返回体与单股一致（`hs`/`v`）——
  本项目两种字段名都认。
* **单股**：`GET /hsrl/ssjy/{code}/{licence}`，返回**单个对象**（不是数组）。

`mairui.spot_mode`（默认 `auto`）决定取数方式：`all` 拿不到就退到 `more`，再退到 `single`（逐只抓）。

## 3. 历史日线

```
GET https://api.mairuiapi.com/hsstock/history/{code}.{MKT}/d/{adjust}/{licence}?st=20250101&et=20250430&lt=100
```

| 参数 | 说明 |
| --- | --- |
| `code.MKT` | 如 `000001.SZ`、`600519.SH`、`920992.BJ` |
| 分时级别 | `5/15/30/60/d/w/m/y`，本项目用 `d`（`mairui.kline_level`） |
| 除权方式 | `n` 不复权 / `f` 前复权 / `b` 后复权 / `fr` 等比前复权 / `br` 等比后复权（`mairui.kline_adjust`，默认 `f`） |
| `st` / `et` | 起止日期 `YYYYMMDD`（也支持 `YYYYMMDDhhmmss`）；不传=全部历史 |
| `lt` | 只取最新 N 条 |

返回 `[{t,o,h,l,c,v,a,pc,sf}, …]`（时间升序）：

```json
[{"a": 980347616.0, "c": 11.65, "h": 11.72, "l": 11.63, "o": 11.7,
  "pc": 11.72, "sf": 0.0, "t": "2025-09-15", "v": 840387.0}]
```

| 字段 | 含义 | 本项目 |
| --- | --- | --- |
| `t` | 交易日（日线是 `YYYY-MM-DD`） | `date` |
| `o` `h` `l` `c` | 开/高/低/收 | 同名 |
| `v` / `a` | 成交量（手）/ 成交额（元） | `volume` / `amount` |
| `pc` | **前收盘价**（注意：实时接口的 `pc` 是涨跌幅，完全不是一回事） | `prev_close` |
| `sf` | 停牌标记，`1`=停牌 | 停牌行**不落库** |

**本地推算的字段**（麦蕊日线不直接给）：

```
chg       = close - pc
pct_chg   = (close - pc) / pc × 100
amplitude = (high - low) / pc × 100
turnover  = volume(手) × 100 / 流通股本(股) × 100%
float_mv  = close × 流通股本
```

流通股本来源（按优先级）：① `symbols.float_shares`，来自每天快照的 `lt/最新价` 反推，
或 `hsstock/instrument` 的 `fv`；② 都没有就留空（`turnover`/`float_mv` 为 NULL，
扫描时用成交额兜底）。用"当前股本"推历史换手率是**近似**：增发/解禁当天的股本变化不会体现，
长周期口径要自行校正。

## 4. 日频技术指标

```
GET .../hsstock/history/{macd|kdj|boll|ma}/{code}.{MKT}/d/{adjust}/{licence}?st=&et=&lt=
```

路径与日线完全同构，参数也一致（`d` + `n/f/b/fr/br` + `st/et/lt`）。返回（日线 `t` 带 ` 00:00:00`）：

| 指标 | 返回字段 | 本项目落库列 |
| --- | --- | --- |
| MACD | `diff` / `dea` / `macd` / `ema12` / `ema26` | `macd_diff` / `macd_dea` / `macd_bar`（EMA 不落） |
| KDJ | `k` / `d` / `j` | `kdj_k` / `kdj_d` / `kdj_j` |
| BOLL | `u` / `m` / `d`（上轨/中轨/下轨） | `boll_upper` / `boll_mid` / `boll_lower` |
| MA | `ma3…ma250` | `ma5` / `ma10` / `ma20` / `ma60`（其余不落） |

```json
// macd
[{"dea": 0.068, "diff": 0.067, "ema12": 11.0782, "ema26": 11.0109,
  "macd": -0.002, "t": "2025-11-27 00:00:00"}]
// kdj
[{"d": 45.01, "j": 22.62, "k": 37.54, "t": "2025-11-27 00:00:00"}]
// boll
[{"d": 10.78, "m": 11.03, "t": "2025-11-27 00:00:00", "u": 11.28}]
```

**别搞混**：BOLL 的 `d` 是**下轨**，KDJ 的 `d` 是 **D 值**，MACD 的 `macd` 是**柱**。
本项目按"接口 + 字段名"分表映射，落库到 `indicators` 表（`(secid, date)` 主键，
四种指标分次写入、互不覆盖）。

## 5. 交易日历

```
GET https://api.mairuiapi.com/tcalendar/list/{year}/{licence}
```

返回升序字符串数组（**`YYYYMMDD`**，本项目统一转成 `YYYY-MM-DD`）：

```json
["20260105", "20260106", "20260107", "20260108", "20260109", "20260112", "..."]
```

实测 2026 年 242 个交易日。这是本项目**自愈回补的标尺**：拿它和本地 `bars` / `indicators`
的日期集合比对，缺哪天就补哪天。未公布节假日的未来年份可能 404，取不到时退回"周一~周五"估算。

## 6. 股票基础信息（可选）

```
GET https://api.mairuiapi.com/hsstock/instrument/{code}.{MKT}/{licence}
```

`{ei, ii, name, od(上市日期), pc, up(涨停价), dp(跌停价), fv(流通股本), tv(总股本), pk, is(停牌状态)}`。

* 用来补 `float_shares` / `listed_date`（`mairui.fetch_instrument: true` 时启用；
  默认关，靠每天快照的 `lt/价格` 反推，零额外请求）。
* 实测该接口**可能返回 503**（`{"code":503,"message":"upstream unavailable"}`），
  属于上游偶发不可用，本项目按单只失败跳过，不影响主流程。

## 7. 踩坑记录（真实踩过的）

* **错误体是 `{"code":503,"message":"upstream unavailable"}`** —— 不是 HTTP 状态码，
  客户端必须解析 body 才算成功。
* **证书无效时可能直接挂住/超时**（不是明确的 401/403）：按"超时→重试→报错"处理，
  报错信息里会提示先核对证书。
* **盘前 `p/v/cje` 全是 0**，但 `t` 已经是今天：本项目把"全 0 快照"当成"数据未更新"，
  不落库不扫描，否则会写出 0 价 0 量的脏 K 线。
* **缺失值是 `null`**（也可能整个字段缺席），不是东财那种 `"-"`；解析统一转 `None`。
* **停牌日**：日线 `sf=1` 且成交量为 0，本项目直接丢弃，避免污染量能基准。
* **实时 `pc`=涨跌幅，日线 `pc`=昨收**：字段名一样、含义完全不同，最容易被坑。
* **指标 `t` 带时分秒**（`2025-11-27 00:00:00`），日线 `t` 只有日期 —— 统一取前 10 位。
* **限频是硬约束**：并发开得再大也突破不了"每分钟 N 次"；本项目用全局最小间隔节流，
  档位填大（超过自己的证书）会被平台拒绝，填小只是慢一点。
* **时区**：接口时间按东八区（`2026-10-13 08:56:59`）。服务器若跑在 UTC，
  北京时间 0:00~8:00 之间"今天"会算错，本项目所有时间判断统一走 `Asia/Shanghai`
  （`emscan/tzutil.py`）。
