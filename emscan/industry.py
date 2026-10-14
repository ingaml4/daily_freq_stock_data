"""行业 / 概念归属（麦蕊《指数、行业、概念》系列接口）。

为什么要它：复盘里"板块"原本只有交易所口径（沪主板/创业板…），讲不出"今天放量集中在电子、
机械设备"，AI 只能写一句"行业维度数据未提供"。麦蕊有现成的板块树：

    hszg/list/{licence}          指数、行业、概念树（叶子节点才有成分股）
    hszg/gg/{节点代码}/{licence}  某个行业/概念下的全部股票（本项目默认用它，按节点批量拉）
    hszg/zg/{股票代码}/{licence}  某只股票挂在哪些行业/概念下（按股票反查，概念多时才有必要）

源数据**每周六**更新，所以这里按周刷新（``industry.refresh_days``，默认 7 天一次），
默认只同步申万一级（31）+ 二级（131）≈ 160 个节点，300 次/分钟的档位约 35 秒跑完。
概念板块（``gn`` / ``chgn`` 共 900+ 个节点）噪音大，需要时在 ``industry.kinds`` 里加上。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .mairui import TAG_KINDS, MairuiClient, MairuiError
from .store import Store
from .tzutil import now, now_str, parse_stamp

log = logging.getLogger("emscan")

META_KEY = "symbol_tags_updated"
DEFAULT_KINDS: Tuple[str, ...] = ("sw", "sw2")


def configured_kinds(cfg, override: Optional[Sequence[str]] = None) -> List[str]:
    """要用哪几类节点：默认申万一级 + 二级。"""
    if override:
        raw: Sequence[Any] = override
    else:
        raw = cfg.get("industry.kinds", None) or list(DEFAULT_KINDS)
    if isinstance(raw, str):
        raw = [part for part in raw.replace("，", ",").split(",")]
    kinds = [str(item).strip() for item in raw if str(item).strip()]
    unknown = [k for k in kinds if k not in TAG_KINDS]
    if unknown:
        log.warning("忽略不认识的板块类别：%s（可用：%s）", ", ".join(unknown), ", ".join(TAG_KINDS))
    known = [k for k in kinds if k in TAG_KINDS]
    return known or list(DEFAULT_KINDS)


def is_due(store: Store, cfg, force: bool = False) -> bool:
    """源数据每周六更新：距上次同步不足 ``refresh_days`` 天就跳过。"""
    if force:
        return True
    days = int(cfg.get("industry.refresh_days", 7) or 7)
    if days <= 0:
        return True
    last = store.get_meta(META_KEY)
    if not last:
        return True
    old = parse_stamp(last)
    if old is None:
        return True
    age = (now() - old).days
    return age >= days


def sync_tags(
    store: Store,
    client: MairuiClient,
    cfg,
    kinds: Optional[Sequence[str]] = None,
    force: bool = False,
    progress=None,
) -> Dict[str, Any]:
    """同步行业/概念归属；返回 ``{kind: 写入行数}`` 与节点数统计。

    返回 ``{}`` 表示"按配置跳过了"（未启用或没到刷新周期）。
    """
    if not bool(cfg.get("industry.enabled", True)) and not force:
        return {}
    wanted = configured_kinds(cfg, kinds)
    if not is_due(store, cfg, force=force):
        log.info("板块归属：距上次同步不足 %s 天，跳过", cfg.get("industry.refresh_days", 7))
        return {}

    nodes = client.fetch_tag_tree(wanted)
    if not nodes:
        raise MairuiError("板块树为空，无法同步板块归属")
    by_kind: Dict[str, List[Dict[str, Any]]] = {}
    for node in nodes:
        by_kind.setdefault(str(node["kind"]), []).append(node)
    log.info(
        "板块归属：%s 个节点（%s）",
        len(nodes),
        "、".join(f"{TAG_KINDS[k]} {len(v)}" for k, v in sorted(by_kind.items())),
    )

    members, errors = client.fetch_tag_members_many(nodes, progress=progress)
    written: Dict[str, int] = {}
    for kind, kind_nodes in by_kind.items():
        rows: List[Dict[str, Any]] = []
        for node in kind_nodes:
            for item in members.get(str(node["code"]), []):
                rows.append(
                    {
                        "secid": item["secid"],
                        "code": node["code"],
                        "name": node["name"],
                    }
                )
        written[kind] = store.replace_symbol_tags(kind, rows)
    if errors:
        log.warning("板块归属：%s 个节点拉取失败（示例 %s）", len(errors), errors[:3])
    store.set_meta(META_KEY, now_str())
    log.info(
        "板块归属完成：%s（股票数 %s）",
        "、".join(f"{TAG_KINDS[k]} {v} 行" for k, v in written.items()),
        store.symbol_tag_stats(),
    )
    return written


def tag_labels(store: Store, kind: str) -> Dict[str, str]:
    """``secid -> 节点名称`` 映射（复盘要用它算行业分布）。"""
    frame = store.load_symbol_tags(kind=kind)
    if frame is None or frame.empty:
        return {}
    return {str(s): str(n) for s, n in zip(frame["secid"], frame["name"])}


def display_map(store: Store, cfg) -> Tuple[str, Dict[str, str]]:
    """展示用的行业口径：取 ``industry.kinds`` 里第一类**有数据**的节点（默认申万一级）。

    没同步过板块归属时返回 ``("", {})``，调用方（复盘/命令行/网页）自动退回"板块口径"。
    """
    try:
        kinds = list(cfg.get("industry.kinds") or ["sw"])
    except Exception:  # pragma: no cover - 配置异常时别把主流程带崩
        kinds = ["sw"]
    for kind in kinds:
        try:
            mapping = tag_labels(store, str(kind))
        except Exception as exc:  # 老库/测试桩没有这张表
            log.debug("读行业归属失败（%s）：%s", kind, exc)
            return "", {}
        if mapping:
            return TAG_KINDS.get(str(kind), str(kind)), mapping
    return "", {}


def describe(store: Store) -> str:
    """给 ``emscan check`` 用的一行说明。"""
    stats = store.symbol_tag_stats()
    if not stats:
        return "未同步（跑 `python3 -m emscan industry` 补齐行业/概念）"
    parts = [f"{TAG_KINDS.get(k, k)} {v['nodes']} 个节点/{v['stocks']} 只" for k, v in sorted(stats.items())]
    return " · ".join(parts) + f"（更新于 {store.get_meta(META_KEY) or '未知'}）"
