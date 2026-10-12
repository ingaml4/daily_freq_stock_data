# A股日频放量扫描（emscan）

盘后用东方财富免费接口落日线行情，每天 17:00 全市场扫一遍，把"突然放量"的票挑出来，
输出终端表格 + CSV/XLSX 报告（可选推送到飞书/企业微信/钉钉/邮件）。

## 现状

已经写好并验证过的部分：

* 数据落库（SQLite 单文件）、增量更新、缺口自动回补、非交易日自动跳过
* 放量算法（量比 5/10/20/60 + 稳健 Z 分数 + 成交额/换手率/历史长度门槛 + ST/停牌剔除）
* 报告输出、可选推送、cron / k8s 部署脚本、接口字段文档
* 32 个单元/集成测试全绿（`python3 -m pytest tests`），以及不联网也能跑的端到端合成数据自检
  （`python3 -m emscan demo`：注入 11 只真实放量 → 11 只全命中，ST/新股/停牌/小成交额样本
  全部按预期剔除，零误报）

**需要你在能上网的机器上跑一次** `python3 -m emscan check --probe`：本机的执行沙箱出不了网，
所以两个东财接口的真实响应我没能亲自验证（解析逻辑是按接口文档 + 固定报文单测写的）。
`--probe` 会打印真实样本，正常情况下你会看到"快照接口 OK / 日K线接口 OK"。

## 快速开始

```bash
cd /home/jumpsrv/daily-freq-stock-data

# 0) 亲眼验证接口（需要能上外网；这一步同时确认字段没变）
python3 -m emscan check --probe

# 1) 首次：回补全市场历史日K线（约 5400 只，几分钟；先用 --limit 100 试也行）
python3 -m emscan init
python3 -m emscan backfill --limit 100     # 试点
python3 -m emscan backfill                 # 全量

# 2) 平日每天跑这一条（增量更新 + 扫描 + 报告）
python3 -m emscan run

# 3) 挂到 cron：每交易日 17:00
bash scripts/daily_scan.sh                 # 先手动试跑一遍
crontab -e                                 # 粘贴 scripts/crontab.example 里的行
```

## 命令一览

| 命令 | 作用 |
| --- | --- |
| `python3 -m emscan init` | 初始化行情库 |
| `python3 -m emscan universe` | 只刷新股票列表（不抓历史） |
| `python3 -m emscan backfill [--limit N] [--start YYYY-MM-DD]` | 回补历史日K线 |
| `python3 -m emscan update [--force]` | 增量更新当天行情 + 缺口回补 |
| `python3 -m emscan scan [--trade-date YYYY-MM-DD]` | 用本地数据扫描（可回看历史某天） |
| `python3 -m emscan run` | 每天 17:00 的入口：update + scan + 报告 + 推送 |
| `python3 -m emscan demo [--symbols N] [--days N]` | 合成行情自检，不联网跑通全链路 |
| `python3 -m emscan check [--probe]` | 自检配置/行情库；`--probe` 联网验证接口 |

## 放量怎么判

对每只股票（当日有成交、非 ST/退市、至少 25 根 K 线）：

```
量比N   = 今日成交量 / 之前 N 个"有效交易日"平均成交量      N = 5, 10, 20, 60
vol_z60 = (log(今日量) - 中位数(log 基准期量)) / (1.4826 × MAD)
```

* 基准期里的停牌日（成交量 = 0）不算数，否则会把量比人为放大（复牌当天能虚高到 6~10 倍）。
* 窗口内有效交易日不足 2 天时该窗口作废（`scan.min_valid_in_window`）；这样"停牌几天后
  复牌放量"能被识别，同时避免用 1 天数据当基准。
* 稳健 Z 分数用中位数 + MAD，对厚尾/异常值不敏感，用来兜住"20 日均值被前期巨量抬高"的情况。

命中规则（可在 `config.yaml` 调）：

```
量比5 >= 2.0  且  (量比20 >= 1.5  或  vol_z60 >= 3.0)
且 成交额 >= 5000 万 元
且 换手率 >= 0.5%
```

命中后按涨跌幅分形态：`放量上攻`（≥+3%）、`放量下跌`（≤-3%）、`放量滞涨`（|涨跌|<1%）、
`放量震荡`（其余），并给一个综合分排序（量比为主，兼顾成交额、换手率、统计显著性）。

## 输出长什么样

```
A股日频放量扫描 | 交易日 2026-10-12
全市场 200 只 / 参与扫描 179 只 / 命中 11 只
命中规则: 量比5 >= 2.0 且 (量比20 >= 1.5 或 Z >= 3.0)；成交额 >= 50000000.0 元，换手率 >= 0.5%，至少 25 根K线

代码    名称     板块    收盘价  涨跌幅%  换手率%  vol_ratio_5  vol_ratio_20  vol_ratio_60  vol_z60  成交额(亿)  形态      评分
------  -------  ------  ------  -------  -------  -----------  ------------  ------------  -------  ----------  --------  -----
600005  测试005  沪主板   89.94    +5.60    80.74         9.47         10.23         10.33      9.3      386.69  放量上攻  19.49
...
```

文件输出（默认）：`data/reports/volscan_<交易日>.csv`（utf-8-sig，Excel 直接打开）和同名 `.xlsx`
（含"放量命中"和"参数与统计"两个 sheet）；在 `report.formats` 里加 `json` 还会输出 JSON。

## 每天 17:00 是怎么跑的

1. **拉全市场快照**（约 54 个请求）→ 拿到股票列表 + 当日成交量/额/换手率。
2. **判断今天是不是交易日**：用快照里的行情时间戳 `f124`；如果日期不是今天（周末/节假日/
   数据没更新），记一条运行日志后**直接退出，不落库不扫描**，退出码 0（不会给 cron 报警）。
3. **落当日行情**：停牌（成交量 0）的票不写当日 K 线。
4. **缺口回补**：把"本地最后一条行情早于上一个交易日"的股票挑出来，用日K线接口从断点补齐
   （比如某天抓取失败、或某只票被漏掉）。同一交易日内补过的票记在 `heal_log` 表里，
   不会反复抓停牌股。上限 500 只/次，剩下留到下次。
5. **扫描 + 出报告 + 推送**，并把本次运行记进 `runs` 表（含命中数、阈值）。

因为 `bars` 表主键是 `(secid, date)` 且全部写入都是 `INSERT OR REPLACE`，**同一天重复跑多少遍
结果都一样**，不会写坏历史。cron 脚本还加了 `flock`，避免上一轮没跑完被重复拉起。

## 配置（config.yaml）

最常调的几个：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `universe.boards` | 沪深主板 + 创业板 + 科创板 | 想加北交所就填上 `bse` |
| `universe.exclude_st` | `true` | 剔除 ST / *ST / 退市整理 |
| `universe.min_bars` | `25` | 过滤新股/次新 |
| `scan.min_amount` | `50000000` | 成交额门槛（元），防小票噪声 |
| `scan.min_turnover` | `0.5` | 换手率门槛（%） |
| `scan.vol_ratio_5_min` / `vol_ratio_20_min` / `z_min` | `2.0` / `1.5` / `3.0` | 命中阈值 |
| `scan.top_n` | `50` | 报告条数上限 |
| `eastmoney.workers` | `8` | 并发抓取线程数，被限流就调小 |
| `notify.enabled` | `false` | 打开后推 webhook/邮件（邮件密码走环境变量，不写配置文件） |

## 目录结构与数据模型

```
emscan/
  eastmoney.py   东财接口客户端（快照 + 日K线，解析函数可单测）
  store.py       SQLite 仓库：symbols / bars / trading_days / runs / heal_log
  scan.py        放量算法（纯计算，不联网）
  report.py      终端表格 + CSV/XLSX/JSON（中文字符按 2 宽度对齐）
  notify.py      webhook（飞书/企微/钉钉/Slack）+ 邮件，失败不影响主流程
  demo.py        合成行情生成器（构造各类边界样本）
  cli.py         命令行入口
scripts/         daily_scan.sh（cron 用）、crontab.example
deploy/          k8s CronJob + PVC 清单（可选）
docs/            东财接口字段与踩坑记录
tests/           32 个测试
data/            market.sqlite + reports/（已 gitignore）
logs/            emscan.log + cron.log（已 gitignore）
```

`bars` 表一行 = 一只票一个交易日：
`secid, date, open, high, low, close, volume(手), amount(元), amplitude, pct_chg, chg,
turnover(%), float_mv, volume_ratio, source`。
`source` 区分数据来源：`spot`（当日快照）/ `kline`（历史回补）/ `demo`（合成）。

数据量参考：5400 只 × 1000 个交易日 ≈ 540 万行，SQLite 文件几百 MB，扫描时只读最近
约 250 个自然日（十几万行），秒级完成。

## 排错

| 现象 | 处理 |
| --- | --- |
| `backfill` 大量失败/超时 | 调小 `eastmoney.workers`（如 4），调大 `timeout`/`retries` |
| 某天 17:00 没出报告，日志写"非交易日或数据未更新" | 正常：当天不是交易日，或东财数据还没更新；确认后可用 `--force` 手动补 |
| 报告里某只票数据明显不对 | 先 `python3 -m emscan check --probe` 确认接口字段没变 |
| 想把历史某天重算一遍 | `python3 -m emscan scan --trade-date 2026-10-09` |
| 想看看库里什么状态 | `python3 -m emscan check`（股票数/行数/日期区间/文件大小） |

## 后续可以加的东西

* 缩量扫描（把判定反过来：量比 ≤ 0.5 的地量股），代码里只需加一个 `mode`
* 连续放量天数、放量后 N 日表现跟踪（数据已经在库里，取数即可）
* 板块/行业共振统计（按 `board` 已经有了，行业需要再抓一次行业分类）
* 涨停梯队、龙虎榜/资金流向叠加
* 接 Airflow / DolphinScheduler 定时，或把结果写入 MySQL/ClickHouse 供看板使用
* 把 `scan.py` 的输入换成任意因子表，就能复用这套"全市场日频扫描 + 报告"的骨架
