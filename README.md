# A股日频放量扫描 + 指标信号（emscan）

盘后用**麦蕊智数**接口落日线行情，每个交易日收盘后（东八区 15:32 / 17:15，可改）全市场扫一遍：

* **放量扫描**：把"突然放量"的票挑出来（量比 + 稳健 Z 分数 + 成交额/换手率门槛）；
* **指标信号**：把"刚进金叉""刚踩布林下轨"的票挑出来（MACD/KDJ/BOLL/MA）；

四个主信号：**突然放量 / MACD 金叉 / KDJ 金叉 / BOLL 下轨突破**，每天按"股票 × 交易日"落一行宽表缓存
（行情 + 指标 + 0/1 标记），`python3 -m emscan hits` 秒查，不用重算也不用重新抓数。

输出终端表格 + CSV/XLSX（可选被 webhook/邮件推送）。**服务器上只需要配一个 licence**，
数据缺口由脚本自己判断、自己补。

## 30 秒上手

```bash
cd /home/jumpsrv/daily-freq-stock-data

export MAIRUI_LICENCE=你的证书              # 到 https://www.mairui.club 免费领取
python3 -m emscan check --probe             # 第 1 步：验证证书/接口/字段（几秒钟）
python3 -m emscan run --limit 50            # 第 2 步：小规模试跑（约 2 分钟）
python3 -m emscan run                       # 第 3 步：全量（首次见下方"耗时"表）

bash scripts/daily_scan.sh                  # 以后每天由 cron 跑这一条
```

第一次跑之前只需要填 licence（写进 `config.yaml` 的 `mairui.licence`，或设环境变量
`MAIRUI_LICENCE`）。库在哪、回补多少天、抓哪些指标、什么时候跳过，全部自动判断。

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `python3 -m emscan run` | **每天跑这一条**：自愈同步 + 放量扫描 + 指标信号 + 报告（+ 推送） |
| `python3 -m emscan check [--probe]` | 自检配置/行情库/指标；`--probe` 联网验证证书与字段 |
| `python3 -m emscan sync [--limit N]` | 只补数据（列表+流通股本+日线+当日行情+指标），不出报告 |
| `python3 -m emscan update` | 只更新当日行情 + 补近期日线缺口 |
| `python3 -m emscan backfill` | 回补日线：新股全历史 + 老股票缺口续传 |
| `python3 -m emscan indicators [--kinds macd,kdj,boll]` | 补齐日频技术指标 |
| `python3 -m emscan scan [--trade-date YYYY-MM-DD]` | 用本地数据做放量扫描（可回看历史某天） |
| `python3 -m emscan signals [--rules ...] [--trade-date ...]` | 用本地数据筛指标信号 |
| `python3 -m emscan box [--trade-date ...]` | 筛**箱体震荡 + 换手健康**的票（做 T 备选池） |
| `python3 -m emscan doji [--trade-date ...]` | 筛**低位缩量十字星**（超跌企稳观察信号） |
| `python3 -m emscan industry [--force] [--kinds sw,sw2]` | 同步**行业/概念归属**（复盘"行业维度"靠它，源数据每周六更新） |
| `python3 -m emscan tiers [--sync] [--days N] [--json] [--out x.csv]` | **复盘梯队回测**：第一/第二梯队的 1/5/10/20 交易日跟踪（含超额） |
| `python3 -m emscan hits [--date ...] [--secid ...] [--only ...]` | **查每日信号缓存**（四个主信号，不联网、秒回） |
| `python3 -m emscan summary [--force] [--force-rule]` | 生成当日复盘总结（有 `DEEPSEEK_API_KEY` 走 AI，没有就出规则版） |
| `python3 -m emscan web [--host ...] [--port ...] [--password ...]` | **起网页端**：四个查询按钮 + 可排序表格（口令登录，代码跳雪球） |
| `python3 -m emscan demo` | 无网络自检：合成行情跑通"落库→扫描→报告" |
| `python3 -m emscan init` | 只初始化行情库 |

常用开关：`--limit N`（本次最多处理 N 只）、`--days N`（自愈窗口）、`--force`（快照日期不是今天也落库）、
`--skip-indicators`、`--skip-signals`。`--config 路径` 是全局参数，要写在子命令**前面**。

## 数据表粒度：一行到底是什么

一共 6 张业务表，粒度和用途都不一样（`data/market.sqlite`，`sqlite3` 直接就能查）：

| 表 | 一行 = | 字段 | 谁写 |
| --- | --- | --- | --- |
| `symbols` | 一只股票 | `secid, code, name, market, board, is_st, float_shares(流通股本), listed_date, updated_at` | 股票列表 + 快照/基础信息 |
| `bars` | **一只股票 × 一个交易日**（日线行情） | `secid, date, open, high, low, close, volume(手), amount(元), amplitude(%), pct_chg(%), chg(元), turnover(%), float_mv, volume_ratio, source` | 当日快照 / 历史回补 |
| `indicators` | **一只股票 × 一个交易日**（技术指标原始值） | `secid, date, macd_diff/macd_dea/macd_bar, kdj_k/kdj_d/kdj_j, boll_upper/boll_mid/boll_lower, ma5/ma10/ma20/ma60, source` | 麦蕊指标接口 |
| `daily_signals` | **一只股票 × 一个交易日（宽表：行情 + 放量 + 指标 + 信号标记）** | 见下 | `scan` 写放量那几列，`signals` 写指标与标记那几列 |
| `symbol_tags` | **一只股票 × 一个行业/概念节点**（归属，源数据每周六更新） | `secid, kind(sw/sw2/gn…), code, name, updated_at` | `industry`（`run` 里按周自动补） |
| `tier_picks` | **一个名单日 × 梯队 × 一只股票**（复盘给的第一/第二梯队） | `date, tier(1/2), secid, code, name, reason, source, created_at` | 复盘生成后自动解析（`tiers --sync` 可补历史） |

`daily_signals` 一行 41 列，就是"当天这只票的全部结论"：

```
secid, date,
close, pct_chg, volume, amount, turnover                       -- 行情
vol_ratio_5/10/20/60, vol_z60, spot_volume_ratio, vol_signal, vol_score   -- 放量指标
macd_diff, macd_dea, macd_bar, kdj_k, kdj_d, kdj_j,
boll_upper, boll_mid, boll_lower, boll_pos, ma5, ma10, ma20, ma60        -- 技术指标数值
s_vol_spike, s_macd_golden, s_kdj_golden, s_boll_break_lower,            -- ① 四个主信号（1/0）
s_macd_dead, s_kdj_dead, s_ma_golden, s_ma_dead,
s_boll_lower, s_boll_upper, s_boll_break_upper,                          -- 其余常用信号（顺手都算了）
computed_at
```

另外有张视图 `daily_signals_named`，把 0/1 标记拼成 `signals` 文本列（如
`vol_spike,macd_golden`），方便任何 SQL 客户端/看板直接读。

## 行业 / 概念归属（复盘里的"行业维度"）

网页的"板块"只有交易所口径（沪主板/创业板/科创板…），讲不出"今天放量集中在电子、机械设备"。
麦蕊有一组板块接口（**不需要 Pro 档**）能补上：

```
python3 -m emscan industry                 # 同步申万一级+二级行业（约 160 个请求，30~60 秒）
python3 -m emscan industry --kinds sw,gn,chgn   # 再加上概念板块 / 热门概念（900+ 节点，约 3 分钟）
python3 -m emscan industry --force         # 没到刷新周期也重拉
```

* 源数据**每周六**更新，所以按 `industry.refresh_days`（默认 7 天）刷新，`emscan run` 会自动补；
  实测申万一级 31 个节点覆盖 5519 只（含北交所）。
* 落 `symbol_tags(secid, kind, code, name)`：一只股票一行一个节点。网页多出一列**行业**（可排序，
  没同步过就不显示这一列），复盘的每个按钮会附**行业分布**，AI 就能回答"是不是一两个行业集体异动"。
* 概念类（`gn`/`chgn` 900+ 节点）噪音大，默认不开；要按概念聚类时再加进 `industry.kinds`。

## 四个主信号：怎么算、什么时候算、能不能缓存

| # | 主信号 | 判定 | 数据来源 |
| --- | --- | --- | --- |
| 1 | 突然放量 | 量比5 ≥ 2.0 且（量比20 ≥ 1.5 或 vol_z60 ≥ 3.0），且成交额 ≥ 5000 万、换手率 ≥ 0.5%、≥ 25 根K线 | 本地 `bars` 纯计算 |
| 2 | MACD 金叉 | 当日 DIFF 上穿 DEA（昨天 DIFF ≤ DEA） | `indicators`（麦蕊 MACD） |
| 3 | KDJ 金叉 | 当日 K 上穿 D | `indicators`（麦蕊 KDJ） |
| 4 | BOLL 下轨突破 | 当日收盘 ≤ 布林下轨（`boll_break_lower`；想用"最低价触及下轨"就把规则换成 `boll_lower`） | `indicators`（麦蕊 BOLL） |

**是每次运行都重算吗？**

* 原始数据（`bars`/`indicators`）**不会重复抓**：每天只补"缺的那一天"（用交易日历比对），
  历史上已经拿到的日期不会二次请求，也不会重算；
* 信号本身是**本地计算**（不联网、毫秒级/秒级），每次 `scan`/`signals` 会把结果**落进 `daily_signals`**；
  `hits` 命令、以后做手机端/看板都**直接读缓存**，不再重算；
* 两个写入者（放量、指标）各写自己那几列，`COALESCE` 合并到同一行，重复跑完全幂等 ——
  所以你可以放心把 `run` 挂两次（见下面"什么时候跑"）。

日常查询（都不联网）：

```bash
python3 -m emscan hits                                  # 最近一个交易日：四个主信号任一命中
python3 -m emscan hits --only macd_golden,kdj_golden     # 当天"同时"金叉的票
python3 -m emscan hits --min-signals 3                   # 至少命中 3 个信号
python3 -m emscan hits --secid 600519 --days 30          # 茅台最近 30 天每天的信号
python3 -m emscan hits --date 2026-10-09 --out /tmp/h.csv
```

也可以直接用 SQL（例：某天同时放量 + MACD 金叉）：

```sql
SELECT secid, date, signals, vol_ratio_5, macd_diff, macd_dea
FROM daily_signals_named
WHERE date = '2026-10-13' AND s_vol_spike = 1 AND s_macd_golden = 1
ORDER BY vol_ratio_5 DESC;
```

## 箱体做T：找"长期磨箱体 + 换手健康"的票

做 T（日内高抛低吸）要的不是趋势股，而是在一个箱体里来回磨、且换手健康的票。
`emscan box` 用最近 **40 个交易日（≈2 个月）** 的日线判断，全部条件满足才算命中：

| 条件 | 默认阈值（`config.yaml` 的 `box:` 段） | 为什么 |
| --- | --- | --- |
| 箱体高度 `(箱顶-箱底)/箱底` | 6% ~ 35% | 太窄做不出差价，太宽就不是箱体 |
| 窗口内净涨跌幅 | \|净涨跌\| ≤ 12% | 单边涨/跌就是在趋势里，不是震荡 |
| 均线缠绕 `\|MA5-MA20\|/MA20` | ≤ 4% | 均线缠在一起才是震荡格局 |
| 日均换手率 | 1% ~ 15% | 太低进出困难，太高是妖股博弈 |
| 换手率变异系数 `std/mean` | ≤ 1.0 | 换手"健康稳定"，不是忽冷忽热 |
| 日均振幅 | 2% ~ 9% | 日内得有波动才有的做 |
| 窗口内 \|涨跌幅\|≥9.5% 天数 | ≤ 2 天 | 连板/天地板不适合做 T |
| 当日成交额 | ≥ 3000 万 | 流动性底线 |
| 有效交易日（非停牌） | ≥ 35 天 | 数据太残缺不看 |

结果按 **箱体位置升序** 排（`box_pos`：0=贴箱底、1=贴箱顶），越靠箱底越靠前；
报告里同时给出箱顶/箱底、箱体高度、日均换手、日均振幅，便于你定高抛低吸价位。
网页端它是第 5 个按钮（**箱体做T**），和放量/MACD金叉/KDJ金叉/布林下轨同级，表头同样可点排序。

```bash
python3 -m emscan box                       # 出报告（box_<交易日>.csv/xlsx）+ 写缓存
python3 -m emscan hits --only box_range      # 只查缓存里的箱体票
```

## 低位缩量十字星：超跌企稳的观察信号

`emscan doji`（网页第 6 个按钮"低位十字星"）用**比较常见的口径**四条同时满足：

| 条件 | 默认（`config.yaml` 的 `doji:` 段可调） | 说明 |
| --- | --- | --- |
| 十字星形态 | 实体 `\|收-开\|/前收 ≤ 0.3%`，且**上下影线**各 ≥ 2 倍实体 | 多空打成平手、盘中有拉锯 |
| 低位 | 收盘在最近 **60 个交易日**区间的位置 ≤ 0.30（或距 60 日高点回撤 ≥ 20%） | 位置越低越好，结果按位置升序排 |
| 缩量 | 当日量 ≤ 之前 **5 个有效交易日均量 × 0.6** | 地量是低位十字星的关键 |
| 可交易 | 成交额 ≥ 2000 万、换手率 ≤ 5%、≥45 根K线、非 ST | 过滤噪声 |

报告/网页给出：`60日位置`、`实体%`、`量/前5日均`、`距60日高点%`，还有当天的收盘/涨跌/成交额/换手。

```bash
python3 -m emscan doji                      # 出报告 + 写缓存
python3 -m emscan hits --only low_doji       # 只查缓存里的低位十字星
```

## 网页端（六个按钮 + 可排序表格）

```bash
python3 -m emscan web                          # 打开 http://127.0.0.1:8080/（默认只本机）
python3 -m emscan web --host 0.0.0.0 --password 我的口令   # 对外暴露：设口令（或走反向代理 + TLS）
```

页面就一件事：把缓存里的信号按天摊开看。结构：

```
┌ 导航： 放量 │ MACD金叉 │ KDJ金叉 │ 布林下轨 ┐   ← 点击切换查询（URL 是 /signals/<视图>）
│ 工具条：日期下拉 + 代码/名称搜索               │
│ 表格  ：代码(→雪球) 名称 板块 收盘价 涨跌幅%    │   ← 表头点击排序
│        成交额(亿) 换手率% 量比5 量比20 Z60 …   │
└ 提示  ：交易日 / 命中只数 / 当前排序 / 搜索词   ┘
```

* **分页**：表格上方和移动端卡片下方各有一条分页栏，**每页 10 / 20 / 50 / 100** 四档可切换，
  显示"共 N 条、当前 x-y 条、第 i / j 页"，带上一页/下一页；页大小用浏览器本地存储记住
  （默认值由 `web.page_size` 配，默认 20）。
* **表头可点击排序（纯前端）**：每列都能点（涨跌幅、成交额、量比、Z 分数、布林位置…），
  点一次切换升降序，**立刻重排、不刷新页面、地址栏保持干净**（URL 里只有视图名和交易日）。
  默认排序按视图给：放量/金叉按**成交额降序**，布林下轨按**布林位置升序**（越跌破下轨越靠前）。
  页面会用浏览器本地存储记住你上次排的列，刷新后自动套用（不想要就设 `web.remember_sort: false`）；
  排序和翻页都是纯前端的（不重新请求、地址栏不变），桌面表格和手机卡片会保持同一顺序。
  另外服务端仍支持 `?sort=…&order=…`（给 `/api/signals` 和脚本用），只是页面不再往 URL 里写。
* **股票代码就是超链接**，直接跳雪球日线行情：`0.000933 → https://www.xueqiu.com/S/SZ000933`、
  `1.601058 → SH601058`、`0.300442 → SZ300442`（京市 `BJ` 也对）。
* **手机适配**：窄屏（≤760px）自动把表格换成**卡片**（一只票一张卡，字段名在上、值在下），
  不用左右拖；导航是横向滚动的胶囊按钮，手指直接点。涨跌用红/绿着色。
* **JSON 接口**：`/api/signals?view=macd_golden&limit=200`，返回同样的数据结构，
  以后做小程序 / 微信 / 大屏看板直接复用；`/healthz` 给探活用。
* **只看最新一天**（日频交易用不到翻历史）：页面永远显示缓存里最新的那个交易日，
  顶部横幅写清「数据日期 / 是不是今天 / 缓存写入时间 / 系统几点更新」——
  数据不是当天时会是醒目的橙色告警，并告诉你下一个更新点（北京时间的 15:32 / 17:15）
  或提示"已过更新时间，今天大概率休市/抓取失败"；搜索框按代码或名称过滤。
  日期选择器已移除；`/api/signals?date=…&sort=…` 仍保留（给脚本/看板用）。

### 部署与安全

* **登录方式**：启动时指定口令（`--password` 或环境变量 `EMSCAN_WEB_PASSWORD`），
  浏览器第一次访问会跳 `/login`，输一次口令后写 Cookie（默认记 7 天，勾"记住我"），
  页面右上角有"退出登录"。口令比较用定长比较、同一 IP 连续失败 8 次会暂时 429，防暴力猜。
* **脚本/接口**：可以用 HTTP Basic 免 Cookie，例如
  `curl -u ":$口令" "http://主机:8080/api/signals?view=macd_golden"`。
* **默认只监听 `127.0.0.1`**（本机）。要手机上访问，两种做法：
  1. **反代出去**（推荐）：`nginx`/`Caddy` 加 TLS，转发到 `127.0.0.1:8080`（反代记得透传 `X-Forwarded-Proto`，会一并给 Cookie 加 `Secure`）；
  2. 临时直连：`--host 0.0.0.0 --password 口令`。注意 http 是明文的，公网务必上 TLS。
* systemd 常驻：一条命令 `sudo scripts/install_services.sh --licence <证书>`（见"部署"一节），
  监听地址/端口/口令都走环境文件里的 `EMSCAN_WEB_HOST` / `EMSCAN_WEB_PORT` / `EMSCAN_WEB_PASSWORD`。
* `/healthz` 不校验口令（只回 `ok`、不含任何数据），方便给 nginx / k8s 探活；其余页面/API 都要登录。
* 只读 SQLite（WAL），所以**页面和 `run` 可以同时跑**，不会互相阻塞。

### 性能：为什么用标准库 `http.server` 就够了

在 5400 只股票 × 5 天（2.7 万行）的缓存库上实测（单机、135 行/页、含移动端卡片渲染）：

| 场景 | 耗时 / 吞吐 |
| --- | --- |
| 单页渲染（冷，含查询+排序+渲染） | **7.5 ms** |
| 单页渲染（暖，命中页面缓存） | **~0.2 ms** |
| 单线程吞吐 | **≈130 req/s** |
| 4 / 8 / 16 并发 | 93 / 137 / 131 req/s，p95 = 39 / 80 / 126 ms |
| gzip（手机流量） | 220 KB → **19 KB**（+1.6 ms CPU） |
| `/api/signals` JSON | 6 ms（100 KB） |

结论与取舍：

1. **够用**：每天只有几十到几百次页面查看（一个人或小团队、手机为主），
   `ThreadingHTTPServer` + 长连接 + gzip 完全撑得住，还少两个依赖、少一个进程管理器。
2. **瓶颈不在 HTTP 层**：这 7.5 ms 里 ~2 ms 是 SQLite 查询、~5 ms 是 HTML 拼接；
   测出来的 GIL 平台期（≈130 req/s）说明是 **CPU 密集的 Python**，不是网络。
   换成 Flask（WSGI，线程模型类似）不会更快；换 FastAPI/uvicorn（async）也只在你**有 I/O 等待**
   （调用外部 API、WebSocket、SSE）时才明显受益 —— 而本服务是"读本地 SQLite + 拼字符串"。
3. **什么时候该换**：并发用户到几十个以上、要在同一进程里做认证/会话/上传、
   或者要把这套查询开放成给多个系统调的 API 平台。那时把 `build_context()` / `render_html()`
   直接挂到 FastAPI 路由上即可（数据层与传输层是分开的，改动约 30 行），
   也可以先不动代码、直接起 2~4 个 `emscan web` 实例用 nginx 做 upstream（无状态只读，天然可横向扩）。

## 每天 `run` 到底做了什么

`run` 是幂等的：脚本自己比对"应该有什么"和"本地有什么"，只补缺的。

```
① 股票列表      /hslt/list                  → symbols（新上市自动进来，ST/退市自动标记）
② 流通股本      快照里的流通市值 ÷ 价格      → symbols.float_shares（历史换手率靠它推算）
③ 当日行情      多股快照(20只/次) → 当日 bars → 停牌不落库；非交易日/数据未更新直接跳过
④ 日线缺口      交易日历 vs 本地 bars        → 缺哪天就从哪天补；没抓过的股票拉全历史
⑤ 指标缺口      交易日历 vs 本地 indicators  → MACD/KDJ/BOLL 各自单独比对、单独补
⑥ 放量扫描      scan.py（纯本地计算）        → volscan_<交易日>.csv/xlsx + 写 daily_signals
⑦ 指标信号      signals.py（纯本地计算）     → signals_<交易日>.csv/xlsx + 写 daily_signals
⑧ 箱体做T       box.py（纯本地计算）          → box_<交易日>.csv/xlsx + 写 daily_signals
⑨ 低位十字星    doji.py（纯本地计算）         → doji_<交易日>.csv/xlsx + 写 daily_signals
```

（③ 排在 ④ 前面是故意的：先用收盘快照把"今天"落库，再比对缺口，就不会为了补今天
给全市场各发一次 K 线请求。）

几个"自己判断"的例子：

* 机器刚装好、库里一条数据都没有 → 步骤 ③ 会给全市场拉全历史（而不是报错）；
* 某天抓取失败、某只票被漏掉 → 下次 `run` 比对交易日历发现缺口，自动补；
* 周末/节假日/数据还没更新（快照日期≠今天）→ 记一条日志直接退出，**不落库、不扫描**，退出码 0（不会惊动 cron）；
* 盘前跑（价格/成交量全是 0）→ 同样跳过，不会写出 0 价 0 量的脏 K 线；
* 证书限频低、一次跑不完 → 用 `--limit` / `sync.max_symbols_per_run` 分多次跑，剩下的下次接着补。
* 长期停牌/退市的票在窗口内永远凑不齐日期 → 每次 `run` 会为它们各发 1 次请求（数量有限、可控），
  换来的是"数据一恢复就自动补上"，不需要人工干预。

所有写入都是 `INSERT OR REPLACE`（`bars`/`indicators` 主键都是 `(secid, date)`），
**同一天重复跑多少遍结果都一样**，不会写坏历史。cron 脚本另外加了文件锁，避免上一轮没跑完被重复拉起。

## 放量怎么判

对每只股票（当日有成交、非 ST/退市、至少 25 根 K 线）：

```
量比N   = 今日成交量 / 之前 N 个"有效交易日"平均成交量      N = 5, 10, 20, 60
vol_z60 = (log(今日量) - 中位数(log 基准期量)) / (1.4826 × MAD)
```

* 基准期里的停牌日（成交量 = 0）不算数，否则会把量比人为放大（复牌当天能虚高到 6~10 倍）。
* 窗口内有效交易日不足 2 天时该窗口作废（`scan.min_valid_in_window`）。
* 稳健 Z 分数用中位数 + MAD，对厚尾/异常值不敏感。

命中规则（`config.yaml` / `emscan/config.py` 可调）：

```
量比5 >= 2.0  且  (量比20 >= 1.5  或  vol_z60 >= 3.0)
且 成交额 >= 5000 万元
且 换手率 >= 0.5%
```

命中后按涨跌幅分形态：`放量上攻`（≥+3%）、`放量下跌`（≤-3%）、`放量滞涨`（|涨跌|<1%）、
`放量震荡`（其余），并给一个综合分排序。

## 指标信号怎么筛

`signals` 在本地指标表上做规则匹配（默认"命中任一"）：

| 规则名 | 含义 | 需要落库的指标 |
| --- | --- | --- |
| `macd_golden` | MACD 金叉：DIFF 上穿 DEA | macd |
| `macd_dead` | MACD 死叉：DIFF 下穿 DEA | macd |
| `kdj_golden` | KDJ 金叉：K 上穿 D | kdj |
| `kdj_dead` | KDJ 死叉：K 下穿 D | kdj |
| `ma_golden` / `ma_dead` | MA5 上穿/下穿 MA10 | ma |
| `boll_lower` | 触及布林下轨（当日最低价 ≤ 下轨） | boll |
| `boll_break_lower` | 收在布林下轨之下（收盘 ≤ 下轨） | boll |
| `boll_upper` / `boll_break_upper` | 触及/收在布林上轨之上 | boll |

默认规则：`macd_golden, kdj_golden, boll_lower`（就是"金叉 + 布林下轨"）。
报告里带 `signals`（规则英文名，方便程序过滤）、`signals_cn`（中文）、`rule_count`（命中几条），
以及 `boll_pos`（布林位置：0=贴下轨、1=贴上轨、<0=跌破下轨），按"命中规则数 → 布林位置 → 成交额"排序。

```bash
python3 -m emscan signals                                   # 默认规则，出 signals_<交易日>.csv/xlsx
python3 -m emscan signals --rules macd_golden,boll_lower     # 换规则
python3 -m emscan signals --trade-date 2026-10-09            # 回看某天
```

## 输出长什么样

```
A股日频放量扫描 | 交易日 2026-10-12
全市场 5202 只 / 参与扫描 4830 只 / 命中 11 只
命中规则: 量比5 >= 2.0 且 (量比20 >= 1.5 或 Z >= 3.0)；成交额 >= 50000000.0 元，换手率 >= 0.5%，至少 25 根K线

代码    名称     板块    收盘价  涨跌幅%  换手率%  vol_ratio_5  vol_ratio_20  vol_ratio_60  vol_z60  成交额(亿)  形态      评分
------  -------  ------  ------  -------  -------  -----------  ------------  ------------  -------  ----------  --------  -----
600005  测试005  沪主板   89.94    +5.60    80.74         9.47         10.23         10.33      9.3      386.69  放量上攻  19.49
```

```
A股日频指标信号 | 交易日 2026-10-12
参与筛选 4830 只 / 命中 37 只
信号规则：MACD金叉 + KDJ金叉 + 触及下轨（任一满足）；上一个交易日 2026-10-09

代码    名称    板块    收盘价  涨跌幅%  ...  命中信号                     信号数  布林位置  布林下轨  DIFF    K     D     J
------  ------  ------  ------  -------  ...  ---------------------------  ------  --------  --------  ------  ----  ----  ----
600001  金叉股  沪主板   10.00    +0.00  ...  MACD金叉、KDJ金叉、触及下轨    3.00      0.00     10.00  -0.050  45.0  38.0  59.0
```

文件输出（默认写在 `data/reports/`）：`volscan_<交易日>.csv|.xlsx`、`signals_<交易日>.csv|.xlsx`
（CSV 为 utf-8-sig，Excel 双击即开；xlsx 带"参数与统计"sheet）。在 `report.formats` 里加 `json` 还会输出 JSON。

## 收盘复盘总结（DeepSeek，可选）

当天数据齐了之后（默认 **17:00**，见 `summary.only_after`），`emscan run` 会把六个按钮的命中汇总成一份
结构化摘要，再交给 DeepSeek 写中文复盘：今日全景、板块与结构、**逐个按钮解读**、多信号共振、
反转信号质量排序、风险提示与可执行的跟踪清单，目标 **1500~3000 字**。
**没配密钥就出"规则版"**（只有本地数据统计，不调 AI）。

给 AI 的数据是**全量**的：`summary.top_symbols: 0`（默认）表示每个按钮有多少只命中就给它多少只，
不做抽样、不截断；每个按钮还附带整体统计（涨跌幅中位数/区间、上涨比例、换手中位数、成交额合计、指标中位数）
和**筛选口径**（和网页悬浮提示同一份，见 `emscan/views.py`），避免 AI 把"放量大涨"理解成放量下跌。
每只票还带**行业**（先跑一次 `python3 -m emscan industry`），所以复盘能直接说清"异动集中在哪几个行业、
占比多少、是扎堆还是全市场散点"，而不再写一句"行业维度数据未提供"。
想省 token 可以把 `top_symbols` 设成正数；再加 `include_universe: true` 还会把"全市场简表"
（缓存里所有股票的涨跌幅/成交额/换手/量比/命中标记）一起喂进去——1M 上下文塞得下，一般不需要。

- 落库 `daily_summary` 表，同时写出 `data/reports/summary_<交易日>.md`
- 网页端顶部有"当日总结"（看正文）和"下载总结"（下载 Markdown）
- 密钥入口（优先级从高到低）：环境变量 `DEEPSEEK_API_KEY`（推荐写进 `/etc/emscan/emscan.env` 或 `.env`）、
  `config.yaml: summary.api_key`、命令行 `--api-key`
- 手动补跑：`python3 -m emscan summary --force`；没到 17:00 也想生成就加 `--force-rule`

### 生成顺序与缓存（重要）

复盘读的是 `daily_signals` 缓存，而 **MACD/KDJ/布林信号是 `signals` 步骤写进去的**。所以 `run` 里的
顺序固定为：扫描 → 箱体 → 十字星 → **指标信号** → **最后才生成总结**。
如果把总结放在信号前面，就会出现"网页上三个按钮有数据、总结里却写 0 只"的假复盘
（2026-10-19 那份就是这个问题），而且会被 `daily_summary` 缓存住。

两道保护：

* **就绪度检查**：当天的指标信号还没写进缓存时，总结会标记为 `pending` 并跳过，不会落一份 0 只的总结
  （`summary.require_indicators: true`；手动生成可加 `--force` 绕过）。
* **数据指纹**：每份总结落库时带一个"行数 + 各视图命中数"的指纹；缓存内容变了会自动重建，
  旧版没有指纹的总结也会在下一次运行时自动重算，不需要手动删库。

只保留 17:15 这一轮 cron 就够了：它会自己完成"补行情 → 算指标 → 写信号 → 出总结"全流程。

### 模型与思考模式（这里有坑）

```yaml
summary:
  model: deepseek-flash      # 或 deepseek-v4-pro（更强、更慢、更贵）
  thinking: true             # 默认开：复盘要跨六个按钮找主线、辨消息面共振
  reasoning_effort: high     # none / low / high / max（none = 关思考）
  max_tokens: 32000          # 生成上限，官方允许 1~393216；填 0 = 不传，用官方默认
  timeout: 240               # 思考模式更慢，非流式一次回完，读超时给足
  only_after: "17:00"        # 几点之后才生成（避开 15:32 那次数据不全）
  top_symbols: 0             # 每个按钮给 AI 看多少只；0 = 全部命中（默认，不抽样）
  include_universe: false    # true = 连"全市场简表"也喂给 AI（prompt 会大很多）
```

三点值得记住：

1. **思考 token 也算进 `max_tokens`**。官方默认是"非思考 8K / 思考 64K / `effort=max` 128K"；
   预算给小了（比如 1600），思考把额度吃光，正文就是空的 —— 这正是之前"生成 0 字"的成因。
   现在默认 32000；想更省可以调小，填 `0` 则交给官方默认值。
2. **`reasoning_effort` 只在开思考时生效**：`none`（= 关思考）/ `low` / `high` / `max`；
   老写法 `medium`、`xhigh` 会映射到 `high`，`minimal` 映射到 `low`。
3. **`temperature` 在思考模式下不生效**（官方文档），只有关掉思考才由它控制发散度。

两道保险都不需要你干预：接口不认 `max_tokens`/`reasoning_effort`（400）→ 自动去掉这两个参数重试；
思考模式返回空正文或被截断（`finish_reason=length`）→ 自动改用非思考模式重试一次，并保留能用的那份。
日志里会写清 `finish_reason`、思考字数、`usage`；落库的 `engine` 带思考档位（如
`deepseek-flash(thinking=high)`），网页上一眼能确认这份复盘到底开没开思考。

`python3 -m emscan check` 会打印当前档位，例如：

```
  当日总结：模型 deepseek-flash · 思考模式 reasoning_effort=high（max_tokens 32000）· 17:00 之后生成 · 密钥 已配置（环境变量/环境文件，长度 35）
```

成本参考：思考 token 按输出计费（`deepseek-flash` 输出 $0.6/1M，off-peak 半价），一份复盘通常几万 token 以内；
官方"峰值时段"是北京时间 09:00–12:00 与 14:00–18:00（周一到周五），17:15 那次生成落在峰值内。

## 复盘梯队回测（第一/第二梯队 1/5/10/20 交易日）

复盘最后会固定输出两行**机器可读**名单（`summary` 提示词里写死了格式）：

```
TIER1: 301591 肯特股份 | 603344 星德胜
TIER2: 301220 亚香股份 | 600316 洪都航空
```

两档的只数上限在 `tiers.max_tier1 / max_tier2`（默认 8 / 15）。提示词里会写明上限，
**解析时也会按上限硬截断**（多出来的会被丢弃并记一条日志），不再只靠模型自觉；
规则版总结（未配 API key）没有梯队名单，正文里会明确说明。

程序在复盘落库后**自动解析**这两行进 `tier_picks` 表（代码会按本地行情回查校验，对不上的丢弃并在
日志/页面上列出来），然后按下面的口径跟踪，用"这个梯队到底赚不赚钱"来打分：

| 口径 | 取值 | 为什么 |
| --- | --- | --- |
| 入场 | **T+1 开盘价**（`backtest.entry: next_open`） | 复盘是收盘后 17:15 生成的，当天买不进去；`close` 是理想化的 T 日收盘买入 |
| 持有 | **1 / 5 / 10 / 20 个交易日**（`backtest.horizons`） | 按交易日算，节假日不会把窗口拉歪 |
| 收益 | 卖出日收盘 ÷ 买入价 − 1 | |
| 基准 | 同一时间窗**全市场等权** | 没有超额就分不清"名单好"还是"当天大盘好" |
| 跟踪中 | 持有期未走完 | 收益留空，不拿没发生的数据充数 |

网页端：`/tiers`（导航栏"梯队回测"）——① 按梯队汇总（均值/胜率/超额/最大回撤）② 按名单日
③ 个股明细（代码可跳雪球），右上角可切"最近 7 天/30 天/90 天/全部"，也能下载 CSV 或用 `/api/tiers`。

命令行：

```bash
python3 -m emscan tiers --sync          # 先从历史复盘里补解析名单，再出报表
python3 -m emscan tiers --days 30 --out /tmp/tiers.csv
python3 -m emscan tiers --json          # 给看板/脚本用
```

注意：**今天之前的老复盘**（提示词还没要求 TIER 行）会用"第一梯队为…"这类句子兜底解析，
能捞到大部分名单，但质量不如新格式；样本少的时候别急着下结论，至少攒够 20~30 个名单日再看胜率。

## 配置

`config.yaml` 默认只有一项必填：

```yaml
mairui:
  licence: ""      # 或 export MAIRUI_LICENCE=...
```

其余全部有默认值，需要时再在 `config.yaml` 里覆盖（默认值与完整注释在 `emscan/config.py` 的 `DEFAULTS`）。
最常调的几项：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `mairui.licence` | `""` | 证书；也可用环境变量 `MAIRUI_LICENCE`（推荐） |
| `mairui.rate_limit_per_min` | `300` | 证书限频：基础 300 / 包月 1000 / 包年 3000 / 钻石 6000 |
| `mairui.workers` | `8` | 并发线程数（不突破上面的限频，只影响"挤满配额"的速度） |
| `mairui.spot_mode` | `auto` | 快照取数：`auto`=多股→逐只（不碰 Pro 专属的全市场接口）；`all` 仅证书含全市场权限时用 |
| `mairui.kline_adjust` | `f` | 前复权（`n` 不复权 / `b` 后复权 / `fr`/`br` 等比） |
| `reconcile_calendar_days` | `45` | 自愈比对窗口：往前多少自然日找缺口 |
| `sync.max_symbols_per_run` | `0` | 单次最多处理多少只（0=不限），限频低时用它分多次跑 |
| `indicators.kinds` | `[macd, kdj, boll]` | 每天抓哪些指标（可选 `ma`）；请求数 = 股票数 × 种类 |
| `indicators.limit` | `10` | 首次抓指标只取最近 N 条（够判断金叉即可） |
| `signals.rules` | `[macd_golden, kdj_golden, boll_lower]` | 信号规则；`signals.match` 可选 `any`/`all` |
| `universe.boards` | 沪深主板+创业板+科创板 | 想加北交所需要另配（见下） |
| `scan.*` | 见上文 | 放量判定阈值（量比、Z、成交额、换手率…） |
| `notify.enabled` | `false` | 打开后把放量命中推到 webhook（飞书/企微/钉钉/Slack）或邮件 |

## 数据源与证书（麦蕊智数）

字段清单、真实报文、踩坑记录见 **[docs/mairui_api.md](docs/mairui_api.md)**；客户端在
[emscan/mairui.py](emscan/mairui.py)（解析函数可单测，不联网也能回归）。

1. **领证书**：<https://www.mairui.club> 免费领取，形如 `LICENCE-XXXX-XXXX-XXXX-XXXX`。
   放到 `mairui.licence` 或环境变量 `MAIRUI_LICENCE`（**别提交进 git**）。
2. **出网放行**：允许访问 `api.mairuiapi.com`（443/HTTPS）。国内机器直连即可，不需要公网 IP。
3. **有企业代理时**（requests 会自动读这两个变量；本机沙箱里代理曾导致偶发 `ProxyError`，
   所以建议把接口域名放进直连名单）：

   ```bash
   export https_proxy=http://10.0.70.30:9997
   export no_proxy=localhost,127.0.0.1,api.mairuiapi.com,api.mairui.club
   ```
4. **按下限节流**：`mairui.rate_limit_per_min` 填自己证书的档位，客户端用全局最小间隔节流，
   不会超频；单个请求失败会自动重试（指数退避），单只失败不影响整批。
5. **验证**：`python3 -m emscan check --probe` 会依次打印 股票列表 / 快照 / 日线 / 3 个指标 /
   交易日历 的真实样本——第一次部署先跑它，比看文档快。

### 证书档位：没有 Pro / 全市场权限也完全能用

麦蕊的**全市场快照接口**（`hsrl/real/all`，一次拿全部 5900+ 只）文档标注只对
**包年/钻石版或 Quant Pro 增强包**开放，且每分钟限 1 次；基础/包月档调它只会拿到 `429`。
本项目**默认不碰它**：

* `mairui.spot_mode: auto`（默认）→ 先走**多股**接口（`hsrl/ssjy_more`，一次 20 只，
  全市场约 260 次请求），拿不到再退到**逐只**（`hsrl/ssjy`）。这两条非 Pro 档就能用；
* 只有你确认证书含"全市场"权限时，才把 `spot_mode` 设成 `all`（省请求但容易被档位卡住）。

所以"是不是 Pro 证书"只影响**当天快照那一步要发多少次请求**，不影响功能与数据完整性
（下面按"指标走接口"估算；默认的本地指标算法**不发这些请求**，见后文"性能优化"）：

| 步骤 | 请求数 | 说明 |
| --- | --- | --- |
| 股票列表 | 1 | 所有档位通用 |
| 当日快照 | ≈股票数 ÷ 20（多股）<br>或 ≈股票数（逐只） | 默认多股；`all` 档位下只要 1 次 |
| 历史日线（补缺口） | 每只股票 1 次（首次） | 之后每天只补缺的那几天 |
| 日频指标 | 股票数 × 指标种类（每天） | 走接口时是每天的大头；默认本地算，**0 次请求** |

### 性能优化（2C2G 小机器也能跑得很快）

先说结论：**慢在接口限频，不在机器**。原来每天要发 `股票数 × 指标种类 ≈ 5400×3 = 1.6 万` 次
指标请求，基础档 300 次/分钟就是 55 分钟。现在把指标改成**本地按日线计算**（`indicators.source: local`，
默认），每天联网请求降到 **约 260 次（只有当日快照）**，一分钟内跑完：

| 阶段 | 旧（指标走接口） | 现（指标本地算） |
| --- | --- | --- |
| 每日 `run` 联网请求 | ≈16200 次 → 300 次/分约 **54 分钟** | ≈262 次 → **1 分钟内** |
| 每日本地计算（5400 只 × 最近 5 个交易日） | — | **约 27 秒**，峰值内存 **119 MB** |
| 首次全量：历史日线 | ≈5400 次 | ≈5400 次（**3 年**，不是 30 年；`sync.history_years`） |
| 首次全量：历史指标 | ≈16200 次（约 54 分钟） | 本地算完全部历史 **约 2.5 分钟**（零请求） |

本地计算用的是标准公式（与通达信/同花顺一致，见 `emscan/indicators.py` 文档字符串）：
`MACD=EMA12/EMA26→DIF/DEA(9)/柱=2×(DIF-DEA)`、`KDJ(9,3,3)`、`BOLL=MA20±2σ`、`MA5/10/20/60`。
想继续用券商口径就设回去：`indicators.source: api`（或临时 `python3 -m emscan indicators --source api`）。

其它提速/省内存的旋钮：

| 配置 / 环境变量 | 作用 |
| --- | --- |
| `mairui.rate_limit_per_min` | **按证书真实档位填**（基础 300 / 体验·包月 1000 / 包年 3000 / 钻石 6000），填对能快 3~10 倍；填大反而会被平台 429 |
| `mairui.workers` | 默认 `0` = 按限频自动推算（300→4、1000→8、3000→24） |
| `indicators.local_days` | 每天只重算最近几天（默认 5，判金叉够用；调到 2 更快） |
| `indicators.local_chunk` | 本地计算每批多少只（默认 400；1G 内存调到 300，峰值内存 ~120MB） |
| `sync.history_years` | 首次回补拉几年（默认 3 年；`0`=从头全量，很慢） |
| `sync.max_symbols_per_run` | 单次最多处理多少只（0=不限）；限频低的证书可以拆成多次跑 |
| `EMSCAN_SQLITE_SYNC=OFF` | 关掉 SQLite 每次提交的 fsync（磁盘忙的机器上建库/写库快一个数量级；行情数据可随时重建） |

**日常只要抓住这三件事：** ① 指标走本地（默认）；② `rate_limit_per_min` 填对档位；
③ 首次回补限定 `history_years: 3`。做完之后 2C2G 的机器每天 `run` 也就一分钟上下。

**北交所**：`universe.boards` 加 `bse` 也不会自动生效——沪深股票列表接口不含北交所，
需要另一份接口（文档里"京市A股数据"）。默认不开。

## 时区（重要）

接口时间、交易日、报告日期全部按**东八区**算，代码里统一走 `emscan/tzutil.py`
（`Asia/Shanghai`），进程启动时还会把 `TZ` 固定成东八区。

**不依赖系统时区**：代码里用的是 `ZoneInfo("Asia/Shanghai")`，进程启动时还会把 `TZ`
强制设成 `Asia/Shanghai`（覆盖系统/继承来的值）。所以哪怕你的机器是 UTC、CST(US)、
或 `/etc/localtime` 是别的时区，本应用判断"今天是不是交易日 / 数据是不是当天的"都用东八区。
唯一能改时区的是环境变量 `EMSCAN_TZ`（比如 `EMSCAN_TZ=UTC`，一般不需要）。

`python3 -m emscan check` 会把两个时钟摆出来给你看，一眼确认没被系统时区带偏：

```
时间：系统 2026-10-13 05:50:27 UTC（TZ=UTC，偏移 +0.0 小时） · 本应用按东八区 2026-10-13 13:50:27
      （同一时刻 UTC 2026-10-13 05:50:27；交易日/数据日期一律按东八区判断，与系统时区无关；要改时区用 EMSCAN_TZ）
      ⚠ 你的系统时区不是东八区（差 +8.0 小时）：本应用不受影响，但你手工看 cron 日志/日期时注意换算
```

你自己写的 cron / k8s `schedule` / systemd `OnCalendar` 仍要按东八区设置（见下）——
那些是"系统级调度"，本应用管不到。

## 什么时间跑（15:30 可以吗？）

可以，但要理解数据什么时候才"齐"：

| 数据 | 麦蕊的更新时间 | 说明 |
| --- | --- | --- |
| 当日快照（收盘价/量/额/换手） | 收盘后即定格（最后一条约 15:29~15:30） | 15:30 就能拿到当天完整的日线行情 |
| 历史分时交易（日线） | 15:30 开始更新，预计 17:10 完成 | 补历史缺口用；当天行情我们走快照，不依赖它 |
| 日频技术指标（MACD/KDJ/BOLL/MA） | 每个交易日 15:35 更新 | **15:30 时当天指标还没有** |

所以推荐**跑两次**（都是幂等的，第二次会把第一次缺的补上）：

```cron
CRON_TZ=Asia/Shanghai
32 15 * * 1-5  /path/to/scripts/daily_scan.sh   # ① 收盘后：当日行情 + 放量扫描（最快看到"突然放量"）
15 17 * * 1-5  /path/to/scripts/daily_scan.sh   # ② 完整：补当天指标 + 金叉/布林信号 + 缓存落库
```

* 15:32 那次会自动**跳过当天指标**（`indicators.publish_time: "15:35"`），不会白跑 3×股票数 次请求；
* 只想跑一次也行，把时间放在 **17:15** 之后（那时行情和指标都齐了），或就留 15:32 ——
  当天指标会在第二天的运行里自动补上（自愈）；
* systemd：`deploy/systemd/emscan-daily.timer` 里两行 `OnCalendar=`（改时间只改这里）；
  k8s：`deploy/k8s-cronjob.yaml` 里有两个 CronJob（15:32 与 17:15），改 `schedule` 即可；
* 想临时补跑/确认：`bash scripts/daily_scan.sh` 或 `python3 -m emscan sync`；
  非交易日会自己跳过，日志里会写清"交易日历：今天不是交易日"。

## 部署

### 方式 A：systemd 服务（推荐，一条命令装好）

`scripts/install_services.sh` 装两个**常规 systemd 服务**（都能 `systemctl enable`），
不用一直开着终端窗口：

| 服务 | 作用 | 管理 |
| --- | --- | --- |
| `emscan-daily.timer` + `emscan-daily.service` | 每交易日 **15:32 / 17:15** 跑 `emscan run`（自愈同步 + 放量扫描 + 指标信号 + 报告/推送） | `systemctl list-timers emscan-daily.timer`、`systemctl start emscan-daily` |
| `emscan-web.service` | 常驻网页端（四个查询按钮 + 可排序表格），崩了自动重启 | `systemctl restart emscan-web`、`journalctl -u emscan-web -f` |

```bash
# ① 装 + enable + 立即启动（写 /etc/systemd/system 与 /etc/emscan/emscan.env，需要 sudo）
sudo scripts/install_services.sh --licence <你的麦蕊证书>

# 先看看它会写什么、做什么（什么都不改、不动 systemd）
scripts/install_services.sh --dry-run --licence <证书>

# 免 root：装成用户级服务（~/.config/systemd/user，用 systemctl --user 管）
scripts/install_services.sh --user --password <网页口令>

# 卸载（环境文件默认保留，想一起删加 --purge）
scripts/install_services.sh --uninstall
```

脚本做的事：渲染 `deploy/systemd/*.in` 模板（替换项目目录 / python 解释器 / 运行用户）→
生成 `0600` 的环境文件（麦蕊证书 + `EMSCAN_WEB_HOST/PORT/TOKEN`）→ `systemd-analyze verify` 校验 →
`systemctl daemon-reload` + `enable --now` → 用 `curl /healthz` 自检网页端是否起来。
重复执行等于"更新配置"（已生成的口令会保留，除非显式传 `--password`）。

常用开关：`--web-host 0.0.0.0`（对外，配合 `--password`）、`--web-port 8080`、
`--daily-times "15:32,17:15"`（写成 `"17:15"` 就是每天只跑一次）、`--python /usr/bin/python3`、
`--no-enable`（只生成文件不 enable）。装完可以先用 `systemctl start emscan-daily.service` 立刻试跑一次。

> 首次部署建议先手动验证：`python3 -m emscan check --probe`、`python3 -m emscan sync --limit 50`。

### 方式 B：cron / k8s（已有运维体系时）

```bash
# cron：crontab -e 后粘贴 scripts/crontab.example 里的两行
bash scripts/daily_scan.sh        # 先手动试跑：增量更新 + 扫描 + 报告 + 推送

# k8s CronJob（跑在已有集群上，零新增服务器；默认也是 15:32 + 17:15 两个 Job）
kubectl apply -f deploy/k8s-cronjob.yaml      # 注意先把 licence 放进 Secret
```

容器里通过环境变量注入证书（`Dockerfile` 已设 `TZ=Asia/Shanghai`）：

```bash
docker build -t emscan:0.1 .
docker run --rm -e MAIRUI_LICENCE=$MAIRUI_LICENCE \
  -v /srv/emscan/data:/data -e EMSCAN_STORE_PATH=/data/market.sqlite emscan:0.1 run
```

### 资源占用与选机器

| 阶段 | 请求数（300 次/分） | 内存峰值 | 说明 |
| --- | --- | --- | --- |
| 首次全量回补 | ≈22000 | ≈300 MB | 日线+指标都靠网络，瓶颈是限频 |
| 每天 `run` | ≈16200 | ≈600 MB | 扫描读回 140 自然日；信号读回 30 日行情+指标 |
| 行情库 | — | — | 5400 只 × 3 年 ≈ 400 万行 ≈ 0.7 GB；加指标 ≈ +0.4 GB |

结论：

* **1 vCPU / 1 GB 内存 / 20 GB 盘**能跑（指标表只留最近窗口的话更省；把
  `scan.lookback_calendar_days` 调到 120 可再降一点内存）。想宽松就 **2 vCPU / 2 GB**。
* 内存占用只取决于"最近多少天 × 股票数"，与历史总长度无关。
* **真正的时间成本是限频**：300 次/分档位下，每天跑完要 ~55 分钟，把它放在 17:00 之后、
  第二天开盘之前都行；要更快就得升证书档位（或减少指标种类）。
* 最省钱：当 k8s CronJob 跑在已有集群上，或挂到任意一台常开 Linux 机器的 cron。

## 目录结构与数据模型

```
emscan/
  mairui.py     麦蕊智数接口客户端（6 类接口，解析函数可单测）
  pipeline.py   自愈同步：按交易日历比缺口，逐项补齐（列表/日线/当日行情/指标）
  store.py      SQLite 仓库：symbols / bars / indicators / trading_days / runs
  scan.py       放量算法（纯计算，不联网）
  signals.py    指标信号筛选（金叉/死叉/布林轨/均线，纯计算）
  report.py     终端表格 + CSV/XLSX/JSON（中文按 2 宽度对齐）
  notify.py     webhook（飞书/企微/钉钉/Slack）+ 邮件
  tzutil.py     东八区时间（所有"今天/现在"的唯一入口）
  demo.py       合成行情生成器（构造各类边界样本）
  cli.py        命令行入口
scripts/         install_services.sh（一键装 systemd 服务）、daily_scan.sh、crontab.example、push_to_github.sh
deploy/          k8s CronJob + PVC、systemd 单元模板（*.in，由 install_services.sh 渲染）
docs/            docs/mairui_api.md（接口字段与踩坑记录）
tests/           74 个单元/集成测试
data/            market.sqlite + reports/（已 gitignore）
logs/            emscan.log + cron.log（已 gitignore）
```

表结构：

* `symbols`：`secid, code, name, market, board, is_st, float_shares, listed_date, updated_at`
* `bars`：一行 = 一只票一个交易日；`secid, date, open, high, low, close, volume(手), amount(元),
  amplitude, pct_chg, chg, turnover(%), float_mv, volume_ratio, source`
  （`source`：`spot` 当日快照 / `kline` 历史回补 / `demo` 合成）
* `indicators`：一行 = 一只票一个交易日；`macd_diff/macd_dea/macd_bar`、`kdj_k/kdj_d/kdj_j`、
  `boll_upper/boll_mid/boll_lower`、`ma5/ma10/ma20/ma60`（四种指标分次写入，互不覆盖）
* `trading_days`：由 bars 去重得到；`runs`：每次运行的账本（模式、股票数、命中数、参数、note）

## 排错

| 现象 | 处理 |
| --- | --- |
| 报错提到 `licence` / 请求超时 | 先跑 `check --probe`：确认证书有效、能访问 `api.mairuiapi.com`、代理是否把域名挡住了 |
| `run` 大量卡在指标阶段 | 那一步就是 3×股票数次请求：按证书档位调 `mairui.rate_limit_per_min`，或减少 `indicators.kinds`，或用 `--limit` 分批 |
| 某天没出报告，日志写"非交易日或数据未更新" | 正常：当天休市，或数据还没更新（日志会带交易日历判断）；确认后可用 `--force` 手动补 |
| `scan` 报"本地没有 XXX 的行情" | 那天确实没有数据（休市/未更新）：先 `run`，或 `scan --trade-date` 指定有数据的交易日 |
| 历史日线的换手率是空 | 缺流通股本：打开 `mairui.fetch_instrument: true` 补一次，或等下一次 `update` 从快照反推 |
| 想知道库里什么状态 | `python3 -m emscan check`（股票数/日线/指标/日期区间/文件大小） |
| 想离线验证逻辑没坏 | `python3 -m emscan demo`（合成行情，11 只注入放量应全命中，ST/新股/停牌/小成交额全剔除） |
| 想看某天/某只票的信号 | `python3 -m emscan hits --date ...` / `--secid 600519 --days 30`（读缓存，不联网） |
| 单元测试跑得慢（几十秒） | 本地磁盘 `fsync` 慢：`EMSCAN_SQLITE_SYNC=OFF python3 -m pytest tests`（行情库随时可重建，测试里已默认关同步写） |

## 开发与测试

```bash
python3 -m pytest tests -q          # 74 个测试：报文解析 / 自愈同步 / 信号 / 缓存 / 网页端 / 报告
python3 -m pytest tests -q -k hits  # 只跑"每日信号缓存 + hits 查询"
python3 -m pytest tests/test_web.py -q   # 只跑网页端（导航/排序/雪球链接/HTTP/口令）
```

测试会在 `/tmp` 下建几十个临时 SQLite 库。共享盘/容器里 `fsync` 可能要 1 秒以上
（表现为"测试像卡住了"），`tests/conftest.py` 已经默认设 `EMSCAN_SQLITE_SYNC=OFF` 让测试快一个数量级；
生产环境不设这个变量（默认 `NORMAL`，保证断电不丢最后几条写入）。

## 后续可以加的东西

* 消息推送升级：把 `signals` 结果也推到手机（webhook 已可用），或做企业微信/自建 Web 的交互查询
* 缩量扫描（把判定反过来：量比 ≤ 0.5 的地量股），代码里只需加一个 `mode`
* 连续放量天数、放量后 N 日表现跟踪、金叉后胜率统计（数据已在库里，取数即可）
* 板块/行业共振统计（按 `board` 已有；行业需要再抓一次分类）
* 涨停梯队、龙虎榜/资金流向叠加（麦蕊有对应接口）
* 把 `scan.py` / `signals.py` 的输入换成任意因子表，复用这套"全市场日频扫描 + 报告"的骨架
