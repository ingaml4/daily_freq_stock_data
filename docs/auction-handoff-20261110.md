# 竞价虚拟字段交接（2026-11-10，北京时间）

## 当前结论

现有证书已经成功调用普通五档 `hsstock/real/five`。这只能证明普通盘口访问可用，不能证明 09:15–09:23 的虚拟参考价、匹配量或未匹配方向权限。当前没有得到供应商明确的竞价字段映射，也没有合格的盘前实时样本，所有竞价专用字段保持 null。不能因买一与卖一价格、数量相同就升级证据。

公开官方材料列出普通五档 ps/pb/vs/vb/t；手数单位的官方说明为 1 手 100 股。`hsstock/lup/auction` 是盘后开收盘竞价结果，不可回填成 09:23 的虚拟量。`hsrl/zbjy` 约 60 分钟更新，不可用作盘前实时成交量。SDK 与 REST 共用鉴权与限流，并不增加数据权限。

官方入口：[沪深 API 文档](https://www.mairui.club/hsdata)、[五档与单位说明](https://www.mairuiapi.com/blog/05-2/)。交易所原始协议即使定义了竞价盘口，也不能证明供应商原样透传。

专门审计子代理核查了[官方 SDK 说明](https://www.mairui.club/sdk)和 [Python client 源码](https://github.com/MaiRuiApi/mairui/blob/main/mairui/client.py#L293)：五档方法只是 HTTP 封装，没有独立竞价解析；SDK 不补足语义证据。五档标注“实时”，每分钟 300/1000/3000/6000 是不同档位的请求限流，不是更新频率或秒级延迟保证。

[Pro 套餐](https://www.mairui.club/packs)没有公开明示的实时虚拟撮合接口；[因子文档](https://www.mairui.club/factordata)的竞价量比仍源于盘后 auction.bp，不解决 09:23 需求。公开收费页面不能证明本账户权限、有效期或额外付费后一定获得虚拟量，故不建议为此直接升级或充值。当前只能证实历史成功的普通五档调用，本次未请求额外收费数据，也未购买。未找到可推荐直接开通的、具有完整官方字段映射与延迟承诺的替代接口。

可继续核实的替代候选为通达信 [TdxAiData 竞价接口](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1hjbidnohpjn4.html)：官方定义 Price/Volume/LeaveQty/Time，且范围为 09:15–09:25；[后台接入](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1hjbgqpdhv114.html)需要数据服务 Key。但其竞价进行中是否立即返回、量单位、LeaveQty 正负对应方向、实际权限/价格和延迟仍未证实，所以当前只是候选，没有集成或购买。Tushare 盘后/09:25 后的竞价接口不适用。

本次没有购买套餐、没有改动本机唯一 09:23 发送者。新增后台采样仅归档，不发送飞书、不改变选股策略。

## 兼容响应契约

路径仍为 `GET /api/auction-snapshot?symbols=SZ300611,SZ300969`，沿用原网页认证。原有顶层 `schema_version="1.0"`、items、raw、book_quality、source_time、received_at、cached、upstream_requests 和 null 竞价快捷字段全部保留。新增顶层和每项 `auction_contract_version="1.1"`。

每个 item 的新增字段：

| 字段 | 含义 |
| --- | --- |
| auction_kind | virtual_opening / opening_result / virtual_closing / ordinary_book_or_closed |
| auction_quality | 当前固定 gap；与普通 book_quality 独立 |
| auction_fields_verified | 当前 false，不能用 book_quality 替代 |
| verified_auction_fields | 当前 []；未来逐字段审核后列出可消费的子集 |
| source_age_seconds | as_of 减上游 source_time；缺上游时间为 null，未来时间为负且不可用 |
| auction_fields | 下表五个字段各自的值、单位、状态及证据 |
| auction_evidence | 厂商映射、实盘窗口验证、刷新间隔、普通盘口权限范围和升级前置条件 |

`auction_fields` 的键与目标归一单位：

| 键 | unit |
| --- | --- |
| indicative_match_price | CNY/share |
| matched_volume | lot |
| matched_amount | CNY |
| unmatched_volume | lot |
| unmatched_direction | buy/sell/none |

这些是契约的目标单位，不代表上游已经提供这些字段。每个字段形状如下，当前所有值均为 null：

```json
{"value":null,"unit":"lot","status":"unverified","reason":"supplier_auction_mapping_unverified","source_fields":[],"evidence":[]}
```

状态规则：

- `unverified`：普通盘口可读，但缺供应商竞价映射。09:25 后 reason 为 `final_match_mapping_unverified`，与盘前虚拟撮合分开。
- `unavailable`：请求失败、旧日期、超过 60 秒、未来时间、阶段不符、无有效报价或字段缺失；reason 沿用具体原因，如 `upstream_error`、`stale_source_time`、`invalid_source_time`、`source_phase_mismatch`、`no_quoted_levels`。无报价并不能单独判定停牌。
- `not_applicable`：非竞价时段，reason 为 `outside_auction_window`。
- 未来只有 `verified` 且 value 非 null 的字段才允许参与对应分析；不能等待未匹配量齐全才使用独立通过验证的参考价或匹配量。当前实现不产生 verified。

旧 `quality` 仍为 gap。买一卖一相等、普通盘口完整、请求成功、同日新鲜、样本有变化，都不单独证明竞价语义。零价格但非零数量仍保留在 raw 中，不能丢弃。

## 明早最小验收入口

固定证券仅 SZ300611 美力科技、SZ300969 恒帅股份。不含 SH688。

```text
python3 -m emscan.auction_validation --expected-date 2026-11-11 --output /home/admin/daily_freq_stock_data/data/auction-validation/2026-11-11.jsonl --sample-once
```

程序只在该日期的 09:15:00–09:22:30 且有效官方交易日历明确为 True 时访问本机 8080 认证 API；日期不符、窗口外、日历未知/休市均零 HTTP。不重试，不接受任意代码或 URL。systemd 模板设置北京时间 2026-11-11 的 09:15:05、09:19:55、09:20:05、09:22:05，Persistent=false；正常四次共最多八个上游五档 GET，缓存命中会减少实际调用。

生产归档为 `data/auction-validation/2026-11-11.jsonl`，权限 0600。每条记录包含无凭证 endpoint/query/request_url、请求与接收时间、HTTP 状态、JSON 响应、原始 UTF-8 响应与 SHA256。HTTP 错误页和异常原文不归档，防止泄漏。

```text
systemctl list-timers --all emscan-auction-validation.timer
journalctl -u emscan-auction-validation.service --since '2026-11-11 09:14:00' --no-pager
```

当前收盘后运行 CLI 只能验证保护性跳过，不创建样本文件；归档写入通过隔离测试验证，不能证明竞价字段可用。明早需分别检查源时间在 09:15–09:23、同日、年龄不超过 60 秒、真实变化与实际延迟；四个稀疏采样不能证明连续刷新频率。只有厂商明确映射和实时样本同时满足要求，才可升级对应字段；否则 09:23 消息仍应明确数据缺口，不把普通委托量或盘后量换名。

主会话负责本机 `auction-watch.py` 的唯一发送者和消费逻辑，本会话只维护兼容 API 与服务器验收归档。

## 发布前验证

2026-11-10 在 Linux Python 3.9 隔离副本运行全套测试：334 passed（包含时间窗、日历、错误脱敏、0600 权限、原响应哈希以及旧接口兼容性），Black 检查通过。`systemd-analyze verify` 验证新增两个 unit，通过解析；首个 OnCalendar 归一为 2026-11-11 09:15:05 CST / 01:15:05 UTC。未调用付费行情接口。实际定时采样和竞价字段认证仍待明早。

## 生产安装结果

代码提交 `9203f9d` 已推送 GitHub 并在生产快进。`emscan-web` 于 2026-11-10 17:21:54（北京时间）重启且 active。`emscan-auction-validation.timer` 已启用且 active，实际 `TimersCalendar` 含上述四个时点，`Persistent=no`。既有 daily 任务未中断，完成状态为 success/0。

闭市验证：认证请求 HTTP 200、无认证 401，契约 1.1、phase=closed、两只证券、upstream_requests=0，所有虚拟字段 null/gap。原响应示例在服务器 `/home/admin/daily_freq_stock_data/data/auction-validation/contract-20261110.json`，明确标注 `postclose_contract_only`，不能充当竞价实时证据。当前 CLI 返回 skipped_window，没有创建 11 日样本文件。明早验收仍看 `2026-11-11.jsonl` 的实际记录，不能把今晚的测试成功写成虚拟量已经可用。
