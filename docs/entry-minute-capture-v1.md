# A/B/C 分钟回放采集契约 v1

保留 D 日收盘后实际生成的 `personal-opportunities/YYYY-MM-DD.json` 字节与 SHA256。用 `date + secid + mode.id` 对齐候选，原样保留 `trigger` 和 `risk`。2026-11-12 的正式归档为 A 0、B 0、C 11。记录采集使用的交易日 D+1、报价源、时间戳来源与成交量单位核验依据，保留可复核的原始响应路径或摘要。

每行一个 UTF-8 JSON 对象。公共字段为 `schema_version: 1`、`type`、`trade_date`、`symbol`、`source`、`received_at`，可加 `raw_ref`。`symbol` 用 SH 或 SZ 加六位代码。`received_at` 是本机收到完整响应的北京时间，保存 `+08:00` 和可用的秒以下精度。跨源合并前核对本机时钟。原始供应商时间与到达时间分别记录。

| `type` | 证据字段与含义 |
| --- | --- |
| `opening` | `source_at`、`open_price`、`auction_verified`。源时点精确到 09:25 且核对为当日撮合时才设 `auction_verified: true`。`auction_shares` 和 `quantity_verified` 仅在有可核实的对手数量和单位时填写。撮合价另列，主对照用同口径卖一代理。 |
| `minute` | `label`、`end`、`close`、`complete`，`low` 可选。区间 `[label, end)` 长 60 秒。`end` 已到且真实到达在其后 60 秒以内才作为候选确认证据。保留首次可用版本，后续修订另留原始证据。 |
| `quote` | `source_at`、`ask_price`、`ask_shares`、`quantity_verified`，`price` 可选。`ask_shares` 用已核实的股数。有效报价的源时点不晚于到达，延迟最多 15 秒。盘口量不足时不假定整笔成交。 |
| `sector` | `key`、`membership_version`、`source_at`、`expected`、`fresh`、`up`、`median_pct`。`symbol` 可为相关证券或 null。`expected` 取当时可见的观察分母。版本来源、明确排除和缺失应能从 `raw_ref` 复核。此证据用于解释主线承接，v1 的买入条件固定。 |
| `coverage` | `start`、`end`、`scope`、`complete`，`reason` 可选。区间为 `[start, end)`，`scope` 为 `minute` 或 `quote`。只有采样计划覆盖整个声明区间、无已知缺测才设 `complete: true`。连续采样记录可以证明采样范围，不能证明逐个交易所报价都已捕获。 |

O 主对照为 09:30:00 至 09:30:15 首个符合条件的卖一代理。R 需要同一连续竞价段内两根相邻完整分钟均严格高于 `trigger`。确认可见时刻取两根首次可用记录的较晚 `received_at`。买入取其后首个合规卖一，报价 `source_at` 不早于该确认时刻，`received_at` 严格更晚。价格低于触发位或触及风险位等情况单列。只有观察到先回落到触发位再收复，才标注回踩；两个完整分钟本身只构成站稳证据。上午和下午分段，分钟不得跨午休拼接。

两组沿用同一候选、风险位、交易资格、资金、费用和滑点。退出共用 D+5 收盘及其后可验证的顺延条件，并遵守 T+1。可见卖一及数量仅构成模拟成交依据。日期或源时点不明的历史分时用于走势诊断；缺覆盖、未到期、已观察到取消分别记录。

## 重跑与读取

```sh
python -m emscan.entry_minute_report \
  --db data/market.sqlite \
  --archive /private/capture/source-opportunities.json \
  --archive-sha256 cd51dd2ef541a148577b5d49d09e495874c18229794af5c08d4aba1001bbd3a4 \
  --capture-config /private/capture/candidate-config.json \
  --events /private/capture/events.jsonl \
  --as-of 2026-11-13T17:20:00+08:00 \
  --out data/strategy-review/entry-minute/2026-11-12
```

`--as-of` 取本次实际已到的可见时点。D+1 可运行入场审核，D+5 收盘数据入库后用相同的归档和事件文件重跑，只更新 `--as-of` 并使用包含最新收盘记录的只读数据库切面。错过成交或受阻持仓仍留在同候选分母中。明确缺口会保留不可评价状态。`report.json` 记录原归档、事件和对应逐笔文件的 SHA256。共享现金按实际观察到的入场时刻分配，同一时刻按代码排序。

`--capture-config` 为可选的原始采集配置。提供时核对配置中的机会归档摘要、D+1 日期、候选键、触发位和风险位，并从冻结的 `boards` 读取板块分母和版本。C 类可记录当时可见的申万二级观察组，A/B 使用正式归档的主线依据。

已登录站点可读 `/api/strategy-review/entry-minute?date=2026-11-12` 和 `/api/strategy-review/entry-minute/orders?date=2026-11-12`，参数是 D 日。HTTP 请求只读取生成的文件。原始采集文件和导入工作目录可放在私有目录。

## 2026-11-16 起的采样覆盖 v2

只有新冻结配置显式包含 `sampling_coverage_version: 2` 时启用。原信号与 O/R 时点条件沿用，旧配置默认 v1，11 月 13 日原始证据不改写。v1 与 v2 的覆盖记录不能默默混用。

v2 将采样是否执行和报价是否可用分别记录。`coverage` 的 `complete` 表示计划采样请求执行成功、该证券响应完整且没有调度空档。必须另外写 `sampling_coverage_version: 2`、`transport_ok`、`symbol_parsed`、`quote_usable`、`current_fresh`，均为明确布尔值。`complete=true` 至少要求 `transport_ok=true` 与 `symbol_parsed=true`。超时、缺证券、解析失败、观察者中断、调度超时仍是不完整区间。

成功收到 09:29 的报价能证明该次采样已执行，但其 `quote_usable=false`，绝不成为 09:30 卖一。报价本身仍按原源时点、收到时点、15 秒新鲜度、数量、价格制度及确认后时序过滤。完整采样不能证明观察到了交易所每一笔报价，成交仍只是显示盘口代理。

v2 按独立来源逐段判断覆盖。一个源失败只否定该源，另一个源确实采到的区间可以补足。两个源都缺失的区间仍会阻断验证。同一源互相冲突的完整/缺失声明，以缺失为准，不允许后续完整声明抹除故障。各源和各观察位置需要独立 source ID。

## 服务器独立备份

`emscan.entry_capture_backup` 用独立、限时的 HTTP 子进程保存公开盘口。盘口与分钟工人相互独立，单个源超时不阻塞其他源。仅用只读交易日历，按冻结标的计划运行。每次重启写新的 `polls-*.jsonl`，原始响应、请求时刻、接收时刻、源时刻、失败和恢复间隔都留存。计划首次启动后保存为 `frozen-plan.json`，重启时不允许偷偷替换。

下一交易日服务器计划放在 `/home/admin/.local/share/emscan/entry-capture/2026-11-16-plan.json`，证据在同级 `2026-11-16/`。`cohorts.recheck` 是原 11 只采集复测，`cohorts.formal` 是前一交易日正式收盘归档的 D+1 研究，顶层 `symbols` 为两者并集。正式归档生成后、服务器首次启动前，由冻结流程原子加入正式队列。未生成归档不能填造正式名单。复测队列不当作新的正式 D+1 信号。

服务器腾讯盘口与分钟连接已实测。新浪在本机可用，服务器出口实测 HTTP 403，因此生产服务器计划禁用新浪，保留本机新浪作为独立来源，不绕过访问限制。服务按 `deploy/emscan-entry-capture-backup.timer` 在 11 月 16 日 09:12 启动，失败 3 秒后重启，并可在同日系统重启后恢复。NTP 与采样心跳均应在盘前检查。独立证据不依赖本机进程过夜存活。

```sh
python -m emscan.entry_backup_export \
  --input /home/admin/.local/share/emscan/entry-capture/2026-11-16 \
  --date 2026-11-16 \
  --quote-out /private/backup-quotes.jsonl \
  --minute-out /private/backup-minutes.jsonl
```

导出清单记录全部输入与输出字节 SHA256。盘口源为 `tencent/server` 或 `sina/server`。备用分钟源为 `tencent-minute/server`，以完整分钟结束标签减一分钟映射到原开始标签，09:30 撮合点不算完整分钟，未完成和超过及时到达期限的分钟不导出为有效证据。该源没有分钟最低价，必须保留 `low=null` 与仅收盘价的风险路径限制。实测不同供应商的部分沪市分钟价格不一致，因此备用分钟单列，不无痕拼接到雪球分钟序列。
