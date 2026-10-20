"""网页端：四个查询按钮 + 可排序表格 + 代码跳雪球（零依赖，只用标准库）。

数据来自本地的每日信号缓存 ``daily_signals``（``scan`` / ``signals`` 跑完就写好了），
所以页面**不联网、不重算**，打开就是秒出；服务只用 ``http.server``，不需要 Flask/FastAPI。

页面结构::

    导航栏：放量 | MACD金叉 | KDJ金叉 | 布林下轨      ← 点击切换查询
    工具条：日期选择 + 代码/名称搜索
    表格  ：代码(跳雪球) 名称 板块 收盘价 涨跌幅 成交额 换手率 量比5 量比20 Z60 布林位置 …
            表头可点击排序（默认按视图给一个合理排序：放量/金叉按成交额，布林按位置）

用法::

    python3 -m emscan web                        # 只监听 127.0.0.1:8080（默认，最安全）
    python3 -m emscan web --host 0.0.0.0 --port 8080 --token 我的口令

手机端：窄屏时表格自动变成"卡片"（每行一张卡片），不用横向拖；导航是横向滚动的胶囊按钮。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import html
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

from .mairui import BOARD_LABELS
from .signals import FLAG_LABELS
from .store import Store
from .tzutil import now_hm, today as tz_today
from .backtest import entry_mode as tiers_entry_mode
from .backtest import report as tiers_report
# 按钮口径只有一份（emscan/views.py）：网页的悬浮提示与复盘给 AI 的口径说明共用
# build_view_help 保留在这里只为兼容旧的 `from emscan.web import build_view_help`
from .views import build_view_help, plain_help  # noqa: F401

log = logging.getLogger("emscan")

# 四个主信号（导航栏就是这四个）
# key -> 标记列 / 导航标题 / 页面标题 / 默认排序字段 / 默认方向
VIEWS: Dict[str, Dict[str, str]] = {
    "vol_spike": {
        "flag": "s_vol_spike",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "vol_ratio_5",
            "vol_ratio_20",
            "vol_z60",
            "vol_signal",
        ),
        "nav": "放量大涨",
        "title": "放量大涨（放量且当日涨幅达标，放量下跌不再展示）",
        "min_pct_chg": True,
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "vol_ratio_5",
            "vol_ratio_20",
            "vol_z60",
            "vol_signal",
        ),
        "sort": "amount_yi",
        "order": "desc",
    },
    "macd_golden": {
        "flag": "s_macd_golden",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "macd_diff",
            "macd_dea",
            "macd_bar",
        ),
        "nav": "MACD金叉",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "macd_diff",
            "macd_dea",
            "macd_bar",
        ),
        "title": "MACD 金叉（DIFF 上穿 DEA）",
        "sort": "amount_yi",
        "order": "desc",
    },
    "kdj_golden": {
        "flag": "s_kdj_golden",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "kdj_k",
            "kdj_d",
            "kdj_j",
        ),
        "nav": "KDJ金叉",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "kdj_k",
            "kdj_d",
            "kdj_j",
        ),
        "title": "KDJ 金叉（K 上穿 D）",
        "sort": "amount_yi",
        "order": "desc",
    },
    "boll_break_lower": {
        "flag": "s_boll_break_lower",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "boll_pos",
            "boll_lower",
            "boll_mid",
            "boll_upper",
        ),
        "nav": "布林下轨",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "boll_pos",
            "boll_lower",
            "boll_mid",
            "boll_upper",
        ),
        "title": "BOLL 下轨突破（收盘 ≤ 下轨）",
        "sort": "boll_pos",
        "order": "asc",
    },
    "reversal": {
        "flag": "s_reversal",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "rev_score",
            "rev_signals",
            "doji_vol_ratio",
            "doji_drawdown",
            "doji_stop_pos",
            "doji_body",
            "doji_lower",
        ),
        "nav": "反转信号",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "rev_score",
            "rev_signals",
            "doji_vol_ratio",
            "doji_drawdown",
            "doji_stop_pos",
            "doji_body",
            "doji_lower",
        ),
        "title": "底部反转信号（评分制：底部上下文 + 量价证据叠加，分越高越强）",
        "sort": "rev_score",
        "order": "desc",
    },
    "box_range": {
        "flag": "s_box_range",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "box_pos",
            "box_width",
            "box_high",
            "box_low",
            "box_avg_turnover",
            "box_avg_amplitude",
        ),
        "nav": "箱体做T",
        "columns": (
            "code",
            "name",
            "board",
            "close",
            "pct_chg",
            "amount_yi",
            "turnover",
            "box_pos",
            "box_width",
            "box_high",
            "box_low",
            "box_avg_turnover",
            "box_avg_amplitude",
        ),
        "title": "箱体震荡 + 换手健康（做 T 备选：越贴箱底越靠前）",
        "sort": "box_pos",
        "order": "asc",
    },
}

DEFAULT_VIEW = "vol_spike"

# 每个按钮的表格列：基础信息 + 该按钮相关的指标（不再所有视图共用一张大表）
BASE_COLUMNS = ("code", "name", "board", "industry", "close", "pct_chg", "amount_yi", "turnover")
VIEW_COLUMNS: Dict[str, tuple] = {
    "vol_spike": BASE_COLUMNS + ("vol_ratio_5", "vol_ratio_20", "vol_z60", "vol_signal"),
    "macd_golden": BASE_COLUMNS + ("macd_diff", "macd_dea", "macd_bar"),
    "kdj_golden": BASE_COLUMNS + ("kdj_k", "kdj_d", "kdj_j"),
    "boll_break_lower": BASE_COLUMNS + ("boll_pos", "boll_lower", "boll_mid", "boll_upper"),
    "box_range": BASE_COLUMNS
    + (
        "box_pos",
        "box_width",
        "box_high",
        "box_low",
        "box_avg_turnover",
        "box_avg_amplitude",
    ),
    "reversal": BASE_COLUMNS
    + (
        "rev_score",
        "doji_vol_ratio",
        "doji_drawdown",
        "doji_stop_pos",
        "doji_body",
        "doji_lower",
        "rev_signals",  # 证据放最后一列（内容长，别挤在中间）
    ),
}

# 表格列：(字段, 表头, 数值格式, 可排序)
COLUMNS: Tuple[Tuple[str, str, Optional[str], bool], ...] = (
    ("code", "代码", None, True),
    ("name", "名称", None, True),
    ("board", "板块", None, True),
    ("industry", "行业", None, True),
    ("close", "收盘价", "{:.2f}", True),
    ("pct_chg", "涨跌幅%", "{:+.2f}", True),
    ("amount_yi", "成交额(亿)", "{:.2f}", True),
    ("turnover", "换手率%", "{:.2f}", True),
    ("vol_ratio_5", "量比5", "{:.2f}", True),
    ("vol_ratio_20", "量比20", "{:.2f}", True),
    ("vol_z60", "Z60", "{:.1f}", True),
    ("boll_pos", "布林位置", "{:.2f}", True),
    ("boll_lower", "布林下轨", "{:.2f}", True),
    ("macd_diff", "DIFF", "{:.3f}", True),
    ("macd_dea", "DEA", "{:.3f}", True),
    ("macd_bar", "MACD柱", "{:.3f}", True),
    ("kdj_k", "K", "{:.1f}", True),
    ("kdj_d", "D", "{:.1f}", True),
    ("kdj_j", "J", "{:.1f}", True),
    ("box_pos", "箱体位置", "{:.2f}", True),
    ("box_width", "箱体高度", "{:.1%}", True),
    ("box_high", "箱顶", "{:.2f}", True),
    ("box_low", "箱底", "{:.2f}", True),
    ("box_avg_turnover", "日均换手%", "{:.2f}", True),
    ("box_avg_amplitude", "日均振幅%", "{:.2f}", True),
    ("doji_pos", "60日位置", "{:.2f}", True),
    ("doji_body", "实体%", "{:.2%}", True),
    ("doji_stop_pos", "收盘位置", "{:.2f}", True),
    ("rev_score", "反转分", "{:.0f}", True),
    ("rev_signals", "证据", None, False),
    ("doji_vol_ratio", "量/前5日均", "{:.2f}", True),
    ("doji_drawdown", "距60日高点%", "{:+.1%}", True),
    ("vol_signal", "放量形态", None, False),
    ("doji_lower", "下影%", "{:.2%}", True),
    ("doji_upper", "上影%", "{:.2%}", True),
    ("signals_cn", "命中信号", None, False),
)

SORTABLE: Tuple[str, ...] = tuple(key for key, _, _, sortable in COLUMNS if sortable)

MARKET_PREFIX = {1: "SH", 0: "SZ", 2: "BJ"}

# 板块筛选用的分组（沪主板/深主板合成"主板"）
BOARD_GROUPS: Dict[str, str] = {
    "沪主板": "主板",
    "深主板": "主板",
    "创业板": "创业板",
    "科创板": "科创板",
    "北交所": "北交所",
}
BOARD_GROUP_ORDER = ("主板", "创业板", "科创板", "北交所")

# 展示列 -> SQL 里真正用来排序的表达式。
# 注意 code/name/board 来自 join 的 symbols 表（别名 s），其余在 daily_signals（别名 d）；
# 成交额展示单位是"亿"，表里存的是"元"。
# 板块按"显示出来的中文名"排（否则 sh_main/科创板/sz_gem 的顺序看起来是乱的）
BOARD_SORT_SQL = (
    "CASE s.board "
    + " ".join(f"WHEN '{key}' THEN '{label}'" for key, label in BOARD_LABELS.items())
    + " ELSE 'zz' END"
)

SYMBOL_SORT_COLUMNS = {"code": "s.code", "name": "s.name", "board": BOARD_SORT_SQL}
SORT_COLUMN_SQL = {"amount_yi": "d.amount"}


def _industry_kind(store: Store) -> str:
    """页面上"行业"列用哪一类节点：优先申万一级，其次二级/证监会行业；没同步过就返回空。"""
    try:
        for kind in ("sw", "sw2", "hangye"):
            row = store.conn.execute(
                "SELECT 1 FROM symbol_tags WHERE kind = ? LIMIT 1", (kind,)
            ).fetchone()
            if row:
                return kind
    except sqlite3.Error as exc:  # 老库没有这张表也能正常出页面
        log.debug("读行业归属失败：%s", exc)
    return ""


class _NullCfg:
    """``make_handler`` 没拿到 Config 时的兜底（测试/嵌入式用法）：一律返回默认值。"""

    def get(self, _key: str, default: Any = None) -> Any:
        return default


def sort_expression(key: str) -> str:
    """把展示列名翻成 SQL 表达式（白名单之外的调用方已挡掉）。"""
    if key == "industry":
        return "industry"  # 行业列来自 join 出来的别名
    if key in SYMBOL_SORT_COLUMNS:
        return SYMBOL_SORT_COLUMNS[key]
    return SORT_COLUMN_SQL.get(key, "d." + key)


# 页面内容只取决于"缓存里有什么"，同一天里反复看同一页不必重算：
# 键 = (库路径, 数据版本, 查询参数)，数据一变（computed_at 变了）自动失效。
# 库路径必须进键：换一个数据库（测试/多库部署）时数据版本可能撞车，缓存会串页。
_CACHE: Dict[Tuple[Any, ...], "PageContext"] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 128

# 表格分页：可选每页行数（前端分页，不重新请求）
PAGE_SIZES: Tuple[int, ...] = (10, 20, 50, 100)
DEFAULT_PAGE_SIZE = 20

# 系统每个交易日自动更新数据的时刻（北京时间）—— 默认值与 install_services.sh 的 --daily-times 一致
DEFAULT_UPDATE_TIMES = "15:32,17:15"

# 两趟各做一半：15:32 收盘快照 + 放量扫描；17:15 技术指标（麦蕊 15:35 才发布）+ 金叉/布林信号
UPDATE_SCHEDULE_NOTE = (
    "系统每个交易日（北京时间，周一至周五）自动更新两次："
    "15:32 收盘行情 + 放量扫描；17:15 技术指标 + 金叉/布林信号。非交易日不更新。"
)


def parse_update_times(raw: str = "") -> List[str]:
    out: List[str] = []
    for part in str(raw or DEFAULT_UPDATE_TIMES).replace("，", ",").split(","):
        item = part.strip()
        if len(item) == 5 and item[2] == ":" and item.replace(":", "").isdigit():
            out.append(item)
    return out or parse_update_times(DEFAULT_UPDATE_TIMES)


def format_update_note(raw: str = "") -> str:
    times = parse_update_times(raw)
    return "系统更新（北京时间）：交易日 " + "、".join(times) + "；非交易日不更新"


def explain_stale(cache_date: str, today: str, raw_times: str = "") -> str:
    """数据不是"当天"时，用一句话说清：现在几点、下一个更新点、大概是休市还是延迟。"""
    times = parse_update_times(raw_times)
    now = now_hm()
    pending = [t for t in times if t > now]
    if cache_date > today:
        return f"缓存里是 {cache_date}（晚于今天 {today}），请检查系统时间。"
    if pending:
        return (
            f"现在北京时间 {now}，{today} 的数据还没跑：下一个更新点是 {pending[0]}。"
            f"想要立刻更新可执行 systemctl start emscan-daily.service"
        )
    return (
        f"现在北京时间 {now}，已经过了今天的更新点（{'、'.join(times)}），"
        f"说明今天大概率不是交易日（周末/节假日），或者抓取失败——"
        f"可看 journalctl -u emscan-daily -n 50 确认"
    )


def _cache_written_at(store: Store, date: str) -> str:
    """这一天缓存最后一次写入的时间（用来判断"数据是刚跑出来的"还是"停在昨天"）。"""
    row = store.conn.execute(
        "SELECT MAX(computed_at) AS t FROM daily_signals WHERE date = ?", (str(date),)
    ).fetchone()
    return str(row["t"] or "") if row else ""


# 登录会话
SESSION_COOKIE = "emscan_session"
DEFAULT_SESSION_DAYS = 7
LOGIN_MAX_FAILURES = 8  # 同一 IP 在窗口内连续失败这么多次就暂时拒绝
LOGIN_WINDOW_SECONDS = 300
_FAILURES: Dict[str, List[float]] = {}
_FAILURES_LOCK = threading.Lock()


def xueqiu_url(secid: str) -> str:
    """secid -> 雪球个股页：``0.000933`` -> https://www.xueqiu.com/S/SZ000933"""
    market_part, _, code = str(secid).partition(".")
    prefix = MARKET_PREFIX.get(int(market_part) if market_part.isdigit() else -1, "SZ")
    return f"https://www.xueqiu.com/S/{prefix}{code}"


def flag_names(flags: Any) -> str:
    """``"vol_spike,macd_golden"`` -> ``"突然放量、MACD金叉"``"""
    names: List[str] = []
    for name in str(flags or "").split(","):
        key = name.strip()
        if key:
            names.append(FLAG_LABELS.get(f"s_{key}", key))
    return "、".join(names)


def normalize_view(view: Optional[str]) -> str:
    key = str(view or "").strip().lower()
    return key if key in VIEWS else DEFAULT_VIEW


def normalize_sort(sort: Optional[str], order: Optional[str], view: str) -> Tuple[str, str]:
    """排序字段/方向做白名单校验，缺省用视图自带的推荐排序。"""
    spec = VIEWS[normalize_view(view)]
    key = str(sort or "").strip()
    if key not in SORTABLE:
        key = spec["sort"]
    direction = str(order or "").strip().lower()
    if direction not in ("asc", "desc"):
        direction = spec["order"]
    return key, direction


@dataclass
class PageContext:
    """渲染一页需要的全部数据（也用于 ``/api/signals`` 的 JSON 输出）。"""

    view: str
    date: Optional[str]
    sort: str
    order: str
    query: str = ""
    rows: List[Dict[str, Any]] = field(default_factory=list)
    total: int = 0
    dates: List[str] = field(default_factory=list)
    title: str = ""
    limit: int = 300
    error: str = ""
    # 数据新鲜度（日频交易只看最新一天，所以这几项要显眼）
    today: str = ""  # 北京时间今天
    stale: bool = False  # 缓存日期 != 今天
    stale_reason: str = ""  # 给用户的解释（含更新时间提示）
    cache_written: str = ""  # 缓存最后一次写入时间
    update_note: str = ""  # "交易日 15:32 / 17:15 更新（北京时间）"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "view": self.view,
            "date": self.date,
            "sort": self.sort,
            "order": self.order,
            "query": self.query,
            "total": self.total,
            "dates": self.dates,
            "today": self.today,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "cache_written": self.cache_written,
            "update_note": self.update_note,
            "rows": self.rows,
        }


def build_context(
    store: Store,
    view: Optional[str] = None,
    date: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    query: str = "",
    limit: int = 300,
    use_cache: bool = True,
    latest_only: bool = True,
    today: Optional[str] = None,
    update_times: str = "",
    vol_up_min_pct: float = 3.0,
) -> PageContext:
    """从缓存里取一页数据：按"日期 + 信号"筛，再搜索、排序、截断。

    全程用 sqlite3 + 纯 Python（不经过 pandas），再套一层"数据版本"缓存：
    单页渲染从 ~26 ms 降到冷态 ~7 ms、暖态 ~0.2 ms（见 README"网页端"一节）。

    ``latest_only=True``（页面用）永远只看**最新一个交易日**，不提供翻历史；
    并算好"数据是不是当天的 / 系统几点更新"这些提示（``stale`` / ``stale_reason``）。
    """
    view_key = normalize_view(view)
    spec = VIEWS[view_key]
    sort_key, sort_order = normalize_sort(sort, order, view_key)
    query = str(query or "").strip()
    limit = max(1, int(limit))
    today = str(today or tz_today())
    update_times = str(update_times or DEFAULT_UPDATE_TIMES)
    page_date = None if latest_only else date

    version = _data_version(store) if use_cache else None
    cache_key = (
        str(store.path),
        version,
        view_key,
        page_date,
        sort_key,
        sort_order,
        query,
        limit,
        today,
        update_times,
    )
    if version is not None:
        with _CACHE_LOCK:
            hit = _CACHE.get(cache_key)
        if hit is not None:
            return hit

    dates = [
        row["date"]
        for row in store.conn.execute(
            "SELECT DISTINCT date FROM daily_signals ORDER BY date DESC LIMIT 400"
        )
    ]
    ctx = PageContext(
        view=view_key,
        date=page_date,
        sort=sort_key,
        order=sort_order,
        query=query,
        dates=dates,
        title=spec["title"],
        limit=limit,
        today=today,
        update_note=format_update_note(update_times),
    )
    if not dates:
        ctx.error = (
            "还没有每日信号缓存：先在服务器上跑一次 `python3 -m emscan run`（或 scan + signals）"
        )
        ctx.stale = True
        ctx.stale_reason = "本地还没有任何缓存数据，页面上暂时没有可看的内容。"
        return ctx
    if ctx.date not in dates:
        ctx.date = dates[0]  # 页面永远看最新的一天
    ctx.stale = str(ctx.date) != today
    if ctx.stale:
        ctx.stale_reason = explain_stale(str(ctx.date), today, update_times)
    ctx.cache_written = _cache_written_at(store, str(ctx.date))

    where = [
        "d.date = ?",
        "COALESCE(d.%s, 0) = 1" % spec["flag"],
        "COALESCE(s.is_st, 0) = 0",  # ST/退市不参与（网页上所有视图统一）
        "COALESCE(d.volume, 1) > 0",  # 明确停牌（volume=0）不显示；NULL 视为"没记录"放行
    ]
    params: List[Any] = [ctx.date]
    if spec.get("min_pct_chg"):  # 放量大涨：只留当日上涨的，放量下跌/滞涨不展示
        where.append("COALESCE(d.pct_chg, -999) >= ?")
        params.append(float(vol_up_min_pct))
    if ctx.query:
        like = "%" + ctx.query.upper() + "%"
        where.append("(UPPER(IFNULL(s.code, '')) LIKE ? OR UPPER(IFNULL(s.name, '')) LIKE ?)")
        params.extend([like, like])
    clause = " AND ".join(where)
    # 行业归属（symbol_tags）左连接：一只股票只取一个节点（MIN+GROUP BY 兜住"概念有多个"的情况）
    industry_kind = _industry_kind(store)
    industry_join = (
        "LEFT JOIN (SELECT secid, MIN(name) AS industry FROM symbol_tags "
        "WHERE kind = ? GROUP BY secid) t ON t.secid = d.secid"
        if industry_kind
        else ""
    )
    source = (
        "FROM daily_signals_named d LEFT JOIN symbols s ON s.secid = d.secid "
        + industry_join
        + " WHERE "
        + clause
    )
    if industry_kind:
        params = [industry_kind] + params

    ctx.total = int(
        store.conn.execute("SELECT COUNT(*) AS n " + source, params).fetchone()["n"] or 0
    )
    if ctx.total:
        sort_sql = sort_expression(ctx.sort)
        direction = "ASC" if ctx.order == "asc" else "DESC"
        rows = store.conn.execute(
            "SELECT d.*, s.code AS code, s.name AS name, s.board AS board, "
            + ("t.industry AS industry " if industry_kind else "NULL AS industry ")
            + source
            + " ORDER BY ("
            + sort_sql
            + " IS NULL), "
            + sort_sql
            + " "
            + direction
            + " LIMIT ?",
            params + [ctx.limit],
        ).fetchall()
        ctx.rows = [_row_payload(row) for row in rows]

    _remember(cache_key, ctx, version)
    return ctx


def _data_version(store: Store) -> str:
    """缓存的数据版本（库里每次写每日信号就 +1）；版本一变，页面缓存整批失效。"""
    return store.data_version()


def _remember(key: Tuple[Any, ...], ctx: "PageContext", version: Optional[str]) -> None:
    if version is None:
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = ctx


def clear_cache() -> None:
    """清空页面缓存（测试、或手工强制刷新时用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# --------------------------------------------------------------------------- 登录
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_password(expected: str, supplied: str) -> bool:
    """口令比对：先各自 sha256 再定长比较，避免长度/时序泄露。"""
    if not expected:
        return False
    return hmac.compare_digest(_sha256(supplied or ""), _sha256(expected))


def _sign(password: str, payload: str) -> str:
    return hmac.new(password.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session(password: str, days: int = DEFAULT_SESSION_DAYS) -> str:
    """签发会话 cookie 值：``到期时间.签名``（签名密钥就是口令本身，改口令即全部失效）。"""
    expires = int(time.time()) + max(1, int(days)) * 86400
    payload = str(expires)
    return f"{payload}.{_sign(password, payload)}"


def check_session(password: str, cookie: Optional[str]) -> bool:
    """校验会话 cookie：签名对得上且没过期。"""
    if not password or not cookie or "." not in cookie:
        return False
    payload, _, signature = cookie.rpartition(".")
    if not payload.isdigit() or int(payload) < time.time():
        return False
    return hmac.compare_digest(signature, _sign(password, payload))


def _login_failures(ip: str) -> int:
    now = time.time()
    with _FAILURES_LOCK:
        stamps = [t for t in _FAILURES.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        _FAILURES[ip] = stamps
        return len(stamps)


def _record_login_failure(ip: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.setdefault(ip, []).append(time.time())


def _clear_login_failures(ip: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.pop(ip, None)


def _safe_next(target: Optional[str]) -> str:
    """只允许跳回本站路径，避免开放重定向。"""
    value = str(target or "").strip()
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value.replace("\r", "").replace("\n", "")


LOGIN_CSS = """\
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e8ecf3;--muted:#98a2b3;--accent:#4aa8ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);min-height:100vh;display:flex;
align-items:center;justify-content:center;padding:20px;
font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",Arial,sans-serif}
form{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:22px 20px;width:100%;max-width:360px}
h1{margin:0 0 4px;font-size:18px}
p.sub{margin:0 0 16px;color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 6px}
input[type=password]{width:100%;padding:11px 12px;font-size:16px;border-radius:10px;
border:1px solid var(--line);background:#12151b;color:var(--fg)}
input[type=password]:focus{outline:none;border-color:var(--accent)}
.row{display:flex;align-items:center;gap:8px;margin-top:10px;color:var(--muted);font-size:13px}
button{margin-top:18px;width:100%;padding:12px;font-size:16px;font-weight:700;
border-radius:10px;border:1px solid var(--accent);background:var(--accent);color:#08121d}
.err{margin-top:12px;padding:9px 10px;border-radius:8px;font-size:13px;
background:#2a1a1a;border:1px solid #5b2a2a;color:#ffb4b4}
.hint{margin-top:14px;color:var(--muted);font-size:12px;text-align:center}
"""


def render_login(title: str, next_path: str = "/", error: str = "", remember: bool = True) -> str:
    """登录页（手机友好：大输入框、单列表单）。"""
    error_html = f'<div class="err">{html.escape(error)}</div>' if error else ""
    checked = " checked" if remember else ""
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · {html.escape(title)}</title><style>{LOGIN_CSS}</style></head>
<body>
<form method="post" action="/login">
  <h1>{html.escape(title)}</h1>
  <p class="sub">请输入访问口令（安装服务时用 --password 指定）</p>
  <label for="password">口令</label>
  <input id="password" name="password" type="password" autocomplete="current-password"
         autofocus required placeholder="口令">
  <div class="row"><input id="remember" name="remember" type="checkbox" value="1"{checked}>
    <label for="remember" style="margin:0">记住我（{DEFAULT_SESSION_DAYS} 天）</label></div>
  <input type="hidden" name="next" value="{html.escape(next_path)}">
  <button type="submit">登录</button>
  {error_html}
  <div class="hint">curl/脚本可直接用 HTTP Basic：curl -u ":$口令" http://主机:端口/api/signals</div>
</form>
</body></html>
"""


def _row_payload(row: Any) -> Dict[str, Any]:
    """把一行 sqlite 记录转成模板要的字典（键与 ``COLUMNS`` 对齐）。"""
    secid = str(row["secid"])
    amount = row["amount"] if "amount" in row.keys() else None
    board_label = BOARD_LABELS.get(str(row["board"]), str(row["board"] or ""))
    item: Dict[str, Any] = {
        "secid": secid,
        "board_group": BOARD_GROUPS.get(board_label, board_label),
        "code": str(row["code"] or secid.split(".")[-1]),
        "name": str(row["name"] or ""),
        "board": board_label,
        "signals_cn": flag_names(row["signals"]),
        "url": xueqiu_url(secid),
        "amount_yi": None if amount is None else float(amount) / 1e8,
    }
    for key in {key for key, _, _, _ in COLUMNS} - set(item):
        if key not in row.keys():
            item[key] = None
            continue
        item[key] = row[key]
    return item


# --------------------------------------------------------------------------- 渲染
CSS = """\
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e8ecf3;--muted:#98a2b3;
--up:#ff5c5c;--down:#3ddc97;--accent:#4aa8ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
header{position:sticky;top:0;z-index:6;background:rgba(15,17,21,.96);
border-bottom:1px solid var(--line);padding:10px 12px 8px}
.brand{font-weight:700;font-size:16px;margin-bottom:8px}
.brand span{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
.brand .logout{float:right;font-weight:400;font-size:12px;color:var(--muted)}
nav{display:flex;gap:8px;overflow-x:auto;padding-bottom:2px}
nav a{flex:0 0 auto;padding:8px 14px;border-radius:999px;background:var(--card);
border:1px solid var(--line);color:var(--fg);white-space:nowrap}
nav a.active{background:var(--accent);border-color:var(--accent);color:#08121d;font-weight:700}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
margin-top:8px;font-size:12px;color:var(--muted)}
.toolbar input,.toolbar button{background:var(--card);color:var(--fg);
border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px}
.toolbar button{background:var(--accent);color:#08121d;font-weight:700;border-color:var(--accent)}
main{padding:10px 12px 40px}
.hint{color:var(--muted);font-size:12px;margin-bottom:8px}
.hint b{color:var(--fg)}
.err{background:#2a1a1a;border:1px solid #5b2a2a;padding:10px;border-radius:8px}
.banner{border-radius:10px;padding:10px 12px;margin-bottom:10px;font-size:13px;
background:#3a2a12;border:1px solid #7a5a1e}
.banner.ok{background:#132a1e;border-color:#245c3a}
.banner .banner-title{font-weight:700;font-size:14px;margin-bottom:4px}
.banner .banner-body{color:var(--fg);opacity:.92}
.banner .banner-note{color:var(--muted);font-size:12px;margin-top:4px}
.toolbar .refresh{padding:6px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--card);color:var(--fg);font-size:13px}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
thead th{position:sticky;top:0;z-index:4;background:var(--card);
text-align:right;white-space:nowrap;padding:8px;border-bottom:1px solid var(--line);
font-size:12px;color:var(--muted)}
thead th::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;
background:var(--line)}
table.stuck thead th{box-shadow:0 4px 8px -6px rgba(0,0,0,.85)}
thead th.left{text-align:left}
thead th a{color:var(--fg);cursor:pointer;user-select:none}
thead th a.active{color:var(--accent);font-weight:700}
tbody td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
tbody td.left{text-align:left}
tr:hover td{background:#1b1f28}
.pos{color:var(--up)}
.neg{color:var(--down)}
.wrap{overflow:auto;-webkit-overflow-scrolling:touch;max-height:min(72vh,760px)}
table td:first-child,table th:first-child{position:sticky;left:0;z-index:2;background:var(--card)}
table td:nth-child(2),table th:nth-child(2){position:sticky;
left:var(--col1-w,66px);z-index:2;background:var(--card)}
thead th:first-child,thead th:nth-child(2){z-index:5}
table td:first-child,table th:first-child{min-width:66px}
.dd{position:relative}
.dd.help{float:right;font-weight:400}
.dd.help summary{font-size:12px;color:var(--muted)}
.dd.help .dd-body{right:0;left:auto;max-width:min(92vw,520px);
white-space:normal;line-height:1.7;color:var(--muted)}
.dd summary{list-style:none;cursor:pointer;padding:6px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--card);color:var(--fg);font-size:13px}
.dd summary::-webkit-details-marker{display:none}
.dd[open] summary{border-color:var(--accent)}
.dd-body{position:absolute;z-index:9;margin-top:6px;min-width:150px;padding:8px 10px;
background:var(--card);border:1px solid var(--line);border-radius:10px;
box-shadow:0 8px 20px -10px #000}
.dd-body label{display:flex;align-items:center;gap:8px;padding:3px 0;color:var(--fg);
font-size:13px;white-space:nowrap}
select{background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:8px;padding:6px 8px;font-size:13px}
.pager{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
justify-content:space-between;
margin:10px 0;padding:8px 10px;background:var(--card);
border:1px solid var(--line);border-radius:10px;
font-size:12px;color:var(--muted)}
.pager .pg-sizes,.pager .pg-nav{display:flex;gap:6px;align-items:center}
.pager a{padding:4px 10px;border:1px solid var(--line);border-radius:8px;
color:var(--fg);cursor:pointer}
.pager a.active{background:var(--accent);border-color:var(--accent);
color:#08121d;font-weight:700}
.pager a.disabled{opacity:.35;cursor:default}
@media (max-width:760px){
  body{font-size:13px}
  thead th,tbody td{padding:6px;font-size:12px}
  table td:nth-child(2),table th:nth-child(2){left:var(--col1-w,60px)}
}
footer{color:var(--muted);font-size:11px;padding:0 12px 24px}
"""

JS = """\
// ① 表头吸顶 + 冻结列宽度：最靠前执行、不依赖后面的逻辑（后面出错也照样生效）
(function () {
  function syncOffsets() {
    var cell = document.querySelector('table tbody td:first-child');
    if (cell) {
      document.documentElement.style.setProperty(
        '--col1-w', cell.getBoundingClientRect().width + 'px'
      );
    }
  }
  syncOffsets();
  window.addEventListener('load', syncOffsets);
  window.addEventListener('resize', syncOffsets);
  window.addEventListener('orientationchange', syncOffsets);
  document.querySelectorAll('details.dd').forEach(function (dd) {
    dd.addEventListener('toggle', syncOffsets);  // 下拉展开也可能改变工具条高度
  });
})();

// ② 表头排序 + 板块多选筛选 + 分页：全在前端（不刷新、地址栏不变），状态用 localStorage 记住。
(function () {
  var ARROW = {asc: '\\u2191', desc: '\\u2193'};
  var SORT_KEY = 'emscan_sort', SIZE_KEY = 'emscan_page_size', BOARD_KEY = 'emscan_boards';
  var REMEMBER = __REMEMBER_SORT__;
  var SIZES = __PAGE_SIZES__;
  var DEFAULT_SIZE = __PAGE_SIZE__;

  var table = document.querySelector('table');
  var all = table && table.tBodies[0] ? Array.prototype.slice.call(table.tBodies[0].rows) : [];
  var boxes = Array.prototype.slice.call(
    document.querySelectorAll('.dd-body input[type=checkbox]')
  );
  var summary = document.getElementById('boardSummary');
  var sizeSel = document.getElementById('pageSize');
  var size = DEFAULT_SIZE, page = 1, on = {};

  function store(key, value) {
    if (REMEMBER) { try { localStorage.setItem(key, value); } catch (e) {} }
  }
  function load(key) {
    if (!REMEMBER) return null;
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  var savedSize = load(SIZE_KEY);
  if (savedSize && SIZES.indexOf(parseInt(savedSize, 10)) >= 0) size = parseInt(savedSize, 10);
  if (sizeSel) sizeSel.value = String(size);

  boxes.forEach(function (box) { on[box.value] = true; });
  var savedBoards = load(BOARD_KEY);
  if (savedBoards) {
    var picked = savedBoards.split(',');
    boxes.forEach(function (box) {
      on[box.value] = picked.indexOf(box.value) >= 0;
      box.checked = on[box.value];
    });
  }

  function boardLabel() {
    var off = boxes
      .filter(function (b) { return !on[b.value]; })
      .map(function (b) { return b.value; });
    if (!boxes.length || !off.length) return '板块：全部';
    if (off.length === boxes.length) return '板块：无';
    var kept = boxes.map(function (b) { return b.value; }).filter(function (v) { return on[v]; });
    return '板块：' + (kept.length <= 2 ? kept.join('+') : kept.length + ' 个');
  }
  function visibleRows() {
    return all.filter(function (row) { return on[row.dataset.board] !== false; });
  }
  function pageCount(n) { return Math.max(1, Math.ceil(n / size)); }
  function clamp(n) { if (page > pageCount(n)) page = pageCount(n); if (page < 1) page = 1; }

  function applyPage() {
    var list = visibleRows();
    clamp(list.length);
    var start = (page - 1) * size, end = start + size;
    for (var i = 0; i < all.length; i++) all[i].style.display = 'none';
    for (var j = start; j < Math.min(end, list.length); j++) list[j].style.display = '';
    document.querySelectorAll('.pager').forEach(function (pager) {
      var total = pager.querySelector('.pg-total');
      if (total) total.textContent = list.length;
      var range = pager.querySelector('.pg-range');
      if (range) {
        var last = Math.min(end, list.length);
        range.textContent = list.length ? (start + 1) + '-' + last + ' 条' : '0 条';
      }
      var label = pager.querySelector('.pg-page');
      if (label) label.textContent = '第 ' + page + ' / ' + pageCount(list.length) + ' 页';
      var prev = pager.querySelector('.pg-prev'), next = pager.querySelector('.pg-next');
      if (prev) prev.classList.toggle('disabled', page <= 1);
      if (next) next.classList.toggle('disabled', page >= pageCount(list.length));
    });
    if (summary) summary.textContent = boardLabel();
  }

  boxes.forEach(function (box) {
    box.addEventListener('change', function () {
      on[box.value] = box.checked;
      page = 1;
      store(BOARD_KEY, boxes.filter(function (b) { return on[b.value]; })
        .map(function (b) { return b.value; }).join(','));
      applyPage();
    });
  });
  if (sizeSel) {
    sizeSel.addEventListener('change', function () {
      size = parseInt(sizeSel.value, 10) || DEFAULT_SIZE;
      page = 1;
      store(SIZE_KEY, size);
      applyPage();
    });
  }
  document.querySelectorAll('.pager [data-page]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      page += (btn.dataset.page === 'next' ? 1 : -1);
      applyPage();
      window.scrollTo({top: 0, behavior: 'smooth'});
    });
  });

  function cellValue(row, index) {
    var cell = row.cells[index];
    if (!cell) return null;
    var text = cell.textContent.trim();
    if (text === '' || text === '-') return null;
    var num = parseFloat(text.replace(/[+%,]/g, ''));
    return isNaN(num) ? text : num;
  }
  function compare(a, b, dir) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    if (typeof a === 'number' && typeof b === 'number') return (a - b) * dir;
    return String(a).localeCompare(String(b), 'zh') * dir;
  }
  function markState(link, key, order) {
    var links = Array.prototype.slice.call(document.querySelectorAll('th a[data-sort]'));
    var label = '';
    links.forEach(function (other) {
      other.classList.toggle('active', other === link);
      var text = other.textContent.replace(/[\\u2191\\u2193\\u2195]\\s*$/, '').trim();
      if (other === link) label = text;
      other.textContent = text + ' ' + (other === link ? ARROW[order] : '\\u2195');
      other.dataset.order = (other === link) ? (order === 'asc' ? 'desc' : 'asc') : 'asc';
    });
    var hint = document.getElementById('sortState');
    if (hint) {
      hint.textContent = label + ' ' + (order === 'asc' ? '升序' : '降序');
      hint.dataset.key = key;
      hint.dataset.order = order;
    }
  }
  function applySort(link, order) {
    var index = link.closest('th').cellIndex;
    var dir = order === 'asc' ? 1 : -1;
    all.sort(function (a, b) { return compare(cellValue(a, index), cellValue(b, index), dir); });
    all.forEach(function (row) { table.tBodies[0].appendChild(row); });
    page = 1;
    applyPage();
    markState(link, link.dataset.sort, order);
    store(SORT_KEY, link.dataset.sort + ':' + order);
  }
  var links = Array.prototype.slice.call(document.querySelectorAll('th a[data-sort]'));
  links.forEach(function (link) {
    link.addEventListener('click', function () {
      if (!table || !table.tBodies[0]) return;
      var order = link.dataset.order === 'desc' ? 'desc' : 'asc';
      try { applySort(link, order); } catch (err) {
        if (window.console && console.warn) console.warn('排序失败', err);
      }
    });
  });

  if (table) {
    var box = table.closest('.wrap');
    if (box) {
      box.addEventListener('scroll', function () {
        table.classList.toggle('stuck', box.scrollTop > 4);
      }, {passive: true});
    }
  }
  // 点下拉外面自动收起
  document.addEventListener('click', function (ev) {
    document.querySelectorAll('details.dd[open]').forEach(function (dd) {
      if (!dd.contains(ev.target)) dd.removeAttribute('open');
    });
  });

  var savedSort = load(SORT_KEY);
  if (savedSort) {
    var parts = savedSort.split(':');
    var match = links.filter(function (item) { return item.dataset.sort === parts[0]; })[0];
    if (match) { try { applySort(match, parts[1] === 'desc' ? 'desc' : 'asc'); } catch (e) {} }
  }
  applyPage();
})();
"""


def _fmt(value: Any, fmt: Optional[str]) -> str:
    if value is None:
        return "-"
    if fmt:
        try:
            return fmt.format(float(value))
        except (TypeError, ValueError):
            return html.escape(str(value))
    return html.escape(str(value))


def _cell_class(key: str, value: Any) -> str:
    if key in ("code", "name", "board", "signals_cn"):
        return "left"
    if key == "pct_chg" and value is not None:
        try:
            return "pos" if float(value) > 0 else ("neg" if float(value) < 0 else "")
        except (TypeError, ValueError):
            return ""
    return ""


def render_html(
    ctx: PageContext,
    title: str = "A股日频信号",
    show_logout: bool = False,
    remember_sort: bool = True,
    page_size: int = DEFAULT_PAGE_SIZE,
    help_text: str = "",
    help_map: Optional[Dict[str, str]] = None,
) -> str:
    """把一页渲染成完整 HTML（服务端渲染，手机/桌面同一份）。"""
    help_map = help_map or {}
    nav = []
    for key, spec in VIEWS.items():
        active = ' class="active"' if key == ctx.view else ""
        tip = plain_help(help_map.get(key, ""))
        tip_attr = (' title="%s"' % html.escape(tip, quote=True)) if tip else ""
        nav.append(
            f'<a{active} href="/signals/{key}?date={html.escape(ctx.date or "")}"{tip_attr}>'
            f'{html.escape(spec["nav"])}</a>'
        )

    view_columns = VIEW_COLUMNS.get(ctx.view) or tuple(k for k, _, _, _ in COLUMNS)
    # 没有同步过行业归属（emscan industry）时不显示空的"行业"列
    if "industry" in view_columns and not any(row.get("industry") for row in ctx.rows):
        view_columns = tuple(key for key in view_columns if key != "industry")
    _by_key = {c[0]: c for c in COLUMNS}
    active_columns = [_by_key[key] for key in view_columns if key in _by_key]
    head_cells = []
    for key, label, _, sortable in active_columns:
        cls = "left" if key in ("code", "name", "board", "signals_cn") else ""
        if sortable:
            # 排序纯前端：表头不带任何链接/参数（地址栏始终只有 view + date）
            nxt = "asc" if not (ctx.sort == key and ctx.order == "desc") else "desc"
            active = ' class="active"' if ctx.sort == key else ""
            arrow = ("↓" if ctx.order == "desc" else "↑") if ctx.sort == key else "↕"
            cell = (
                f'<th class="{cls}"><a{active} data-sort="{key}" data-order="{nxt}" '
                f'role="button" tabindex="0">{html.escape(label)} {arrow}</a></th>'
            )
        else:
            cell = f'<th class="{cls}">{html.escape(label)}</th>'
        head_cells.append(cell)

    body_rows = []
    for row in ctx.rows:
        cells = []
        for key, label, fmt, _ in active_columns:
            if key == "code":
                cells.append(
                    f'<td class="left"><a href="{html.escape(row["url"])}" target="_blank" '
                    f'rel="noopener">{html.escape(row["code"])}</a></td>'
                )
            elif key == "signals_cn":
                cells.append(f'<td class="left">{_fmt(row.get(key), None)}</td>')
            else:
                value = row.get(key)
                cells.append(f'<td class="{_cell_class(key, value)}">{_fmt(value, fmt)}</td>')
        body_rows.append(
            f'<tr data-sid="{html.escape(row["secid"])}" '
            f'data-board="{html.escape(str(row.get("board_group") or ""))}">'
            + "".join(cells)
            + "</tr>"
        )

    order_cn = "升序" if ctx.order == "asc" else "降序"
    sort_label = dict((key, label) for key, label, _, _ in COLUMNS).get(ctx.sort, ctx.sort)
    written = ctx.cache_written or "-"
    hint = (
        f"数据日期 <b>{html.escape(ctx.date or '-')}</b>"
        f"（今天 {html.escape(ctx.today or '-')}）· 缓存写入 <b>{html.escape(written)}</b> · "
        f"命中 <b>{ctx.total}</b> 只（显示前 {min(ctx.total, ctx.limit)} 只）· 排序："
        f'<b id="sortState" data-key="{html.escape(ctx.sort)}" '
        f'data-order="{html.escape(ctx.order)}">{html.escape(sort_label)} {order_cn}</b>'
    )
    if ctx.query:
        hint += f" · 搜索：<b>{html.escape(ctx.query)}</b>"
    banner = ""
    if ctx.stale and not ctx.error:
        banner = (
            '<div class="banner">'
            f'<div class="banner-title">⚠ 数据不是当天的：显示的是 '
            f'{html.escape(str(ctx.date or "-"))} 的缓存</div>'
            f'<div class="banner-body">{html.escape(ctx.today)} 还没有数据。'
            f"{html.escape(ctx.stale_reason)}</div>"
            f'<div class="banner-note">{html.escape(UPDATE_SCHEDULE_NOTE)}</div>'
            "</div>"
        )
    elif not ctx.error:
        banner = (
            '<div class="banner ok">'
            f'<div class="banner-title">✔ 数据就是今天的（{html.escape(str(ctx.date or "-"))}）'
            f"，缓存写入 {html.escape(written)}</div>"
            f'<div class="banner-note">{html.escape(UPDATE_SCHEDULE_NOTE)}</div>'
            "</div>"
        )
    body = (
        f'<div class="err">{html.escape(ctx.error)}</div>'
        if ctx.error
        else (
            ""
            if not ctx.rows
            else (
                '<div class="wrap"><table><thead><tr>'
                + "".join(head_cells)
                + "</tr></thead><tbody>"
                + "".join(body_rows)
                + "</tbody></table></div>"
            )
        )
    )
    empty = "" if ctx.error or ctx.rows else '<p class="hint">这一天没有命中该信号。</p>'
    js_rendered = (
        JS.replace("__REMEMBER_SORT__", "true" if remember_sort else "false")
        .replace("__PAGE_SIZES__", "[" + ",".join(str(x) for x in PAGE_SIZES) + "]")
        .replace("__PAGE_SIZE__", str(int(page_size)))
        .replace("__TOTAL__", str(int(ctx.total)))
    )
    pager = _render_pager(int(page_size)) if ctx.rows else ""
    groups = [g for g in BOARD_GROUP_ORDER if any(r.get("board_group") == g for r in ctx.rows)]
    board_filter = _render_board_filter(groups)
    pager_block = pager
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · {html.escape(ctx.title or "")}</title>
<style>{CSS}</style></head>
<body>
<header>
  <div class="brand">{html.escape(title)}<span>{html.escape(ctx.title or "")}</span>
    {('<details class="dd help"><summary title="筛选口径">筛选口径 ?</summary>'
      f'<div class="dd-body">{help_text}</div></details>') if help_text else ""}
    {('<a class="logout" href="/logout">退出登录</a>') if show_logout else ""}</div>
  <nav>{"".join(nav)}</nav>
  <form class="toolbar" method="get" action="/signals/{ctx.view}">
    <input name="q" value="{html.escape(ctx.query)}" placeholder="代码/名称" size="10">
    <button type="submit">查询</button>
    <a class="refresh" href="/signals/{ctx.view}">刷新</a>
    <a class="refresh" href="/summary" title="收盘后（17:00 之后）自动生成的当日复盘">当日总结</a>
    <a class="refresh" href="/summary.md" download title="下载 Markdown">下载总结</a>
    <a class="refresh" href="/tiers" title="复盘第一/第二梯队的 1/5/10/20 交易日跟踪回测">梯队回测</a>
    {board_filter}
  </form>
</header>
<main>
  {banner}
  <div class="hint">{hint}</div>
  {pager_block}
  {body}{empty}
</main>
<footer>数据来自本地每日信号缓存（daily_signals）· 代码点击跳转雪球行情 · 表头可点击排序</footer>
<script>{js_rendered}</script>
</body></html>
"""


def render_json(ctx: PageContext) -> str:
    return json.dumps(ctx.as_dict(), ensure_ascii=False, indent=2)


# --------------------------------------------------------------- 梯队回测页面
TIERS_CSS = """\
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#e8ecf3;--muted:#98a2b3;
--up:#ff5c5c;--down:#3ddc97;--accent:#4aa8ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
header{position:sticky;top:0;z-index:5;background:rgba(15,17,21,.96);
border-bottom:1px solid var(--line);padding:10px 12px}
main{padding:12px 12px 40px}
h1{margin:0 0 6px;font-size:17px}
h2{margin:20px 0 8px;font-size:15px}
.hint{color:var(--muted);font-size:12.5px}
.toolbar{padding:8px 12px;border-bottom:1px solid var(--line);font-size:12.5px;
color:var(--muted);display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.toolbar a,.toolbar span.on{padding:5px 9px;border:1px solid var(--line);border-radius:8px}
.toolbar span.on{border-color:var(--accent);color:var(--fg)}
.wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:right}
th{position:sticky;top:0;background:#12151b;color:var(--muted);font-weight:600;z-index:2}
th:first-child,td:first-child,th.l,td.l{text-align:left}
tr:hover td{background:#141821}
.pos{color:var(--up)}.neg{color:var(--down)}.muted{color:var(--muted)}
.tag{display:inline-block;padding:1px 6px;border-radius:6px;border:1px solid var(--line);
font-size:12px;color:var(--muted)}
.t1{border-color:#7a5a1e;color:#ffd479}.t2{border-color:#245c3a;color:#7fe0ab}
.banner{border-radius:10px;padding:10px 12px;margin:10px 0;font-size:13px;
background:#3a2a12;border:1px solid #7a5a1e}
footer{margin-top:24px;color:var(--muted);font-size:12px}
"""

TIERS_DAY_OPTIONS: Tuple[Tuple[int, str], ...] = (
    (7, "最近 7 个名单日"),
    (30, "30 天"),
    (90, "90 天"),
    (0, "全部"),
)


def _pct(value: Any, digits: int = 2) -> str:
    """百分比单元格：正红负绿（A 股习惯），空值给个灰点。"""
    if value is None:
        return '<span class="muted">-</span>'
    number = float(value) * 100.0
    cls = "pos" if number > 0 else ("neg" if number < 0 else "muted")
    return f'<span class="{cls}">{number:+.{digits}f}%</span>'


def render_tiers_page(
    result: Dict[str, Any],
    *,
    title: str = "A股日频信号",
    days: int = 90,
    entry: str = "next_open",
) -> str:
    """梯队回测页：按梯队汇总 + 按名单日 + 个股明细，手机上也能横向滑动看。"""
    horizons = [int(h) for h in (result.get("horizons") or [])]
    entry_cn = "T+1 开盘买入" if entry == "next_open" else "T 日收盘买入"
    hold_cn = "、".join(f"{h}" for h in horizons)

    day_links = []
    for value, label in TIERS_DAY_OPTIONS:
        if value == days:
            day_links.append(f'<span class="on">{html.escape(label)}</span>')
        else:
            day_links.append(f'<a href="/tiers?days={value}">{html.escape(label)}</a>')
    toolbar = (
        '<div class="toolbar"><a href="/">← 信号页</a>'
        + "".join(day_links)
        + f'<a href="/tiers.csv?days={days}" download>下载 CSV</a>'
        + f'<a href="/api/tiers?days={days}">JSON</a></div>'
    )

    hint = (
        f'<p class="hint">口径：<b>{entry_cn}</b>（复盘是收盘后生成的，当天买不进去）· '
        f"持有 <b>{hold_cn}</b> 个交易日（收益 = 卖出日收盘 ÷ 买入价 − 1）· "
        "对照基准 = 同一时间窗<b>全市场等权</b>收益，超额 = 个股 − 基准 · "
        "「跟踪中」= 持有期还没走完，不拿没发生的数据充数。</p>"
    )

    rows = result.get("rows") or []
    if not rows:
        body = (
            '<div class="banner">还没有梯队名单。复盘（17:15 那次）生成后会自动解析文末的 '
            "<code>TIER1/TIER2</code> 两行；历史复盘也可以补："
            "<code>python3 -m emscan tiers --sync</code>。</div>"
        )
        return _tiers_page_shell(title, toolbar, hint + body, "")

    # ① 按梯队汇总（这正是"哪个梯队真有用"的答案）
    head = '<tr><th class="l">梯队</th><th>样本</th>' + "".join(
        f"<th>{h}日 均值</th><th>胜率</th><th>超额</th>" for h in horizons
    ) + "<th>最大回撤均值</th><th>期间最大涨幅均值</th></tr>"
    lines = []
    for item in (result.get("by_tier") or {}).values():
        tier_cls = int(item["tier"]) if int(item["tier"]) in (1, 2) else 2
        cells = [
            f'<td class="l"><span class="tag t{tier_cls}">'
            f'{html.escape(str(item["label"]))}</span></td>',
            f'<td>{item["picks"]} <span class="muted">(完成 {item["finished"]})</span></td>',
        ]
        for horizon in horizons:
            stats = item.get(str(horizon)) or {}
            if not stats.get("n"):
                cells.extend(["<td>-</td>", "<td>-</td>", "<td>-</td>"])
                continue
            cells.append(f"<td>{_pct(stats.get('avg'))}</td>")
            cells.append(f'<td>{stats["win_rate"] * 100:.0f}%</td>')
            cells.append(f"<td>{_pct(stats.get('avg_excess'))}</td>")
        cells.append(f'<td>{_pct(item.get("avg_max_drawdown"), 1)}</td>')
        cells.append(f'<td>{_pct(item.get("avg_max_gain"), 1)}</td>')
        lines.append("<tr>" + "".join(cells) + "</tr>")
    by_tier_table = f'<div class="wrap"><table>{head}{"".join(lines)}</table></div>'

    # ② 按名单日
    head = '<tr><th class="l">名单日</th><th class="l">梯队</th><th>只数</th>' + "".join(
        f"<th>{h}日 均值</th><th>{h}日 超额</th>" for h in horizons
    ) + "<th>状态</th></tr>"
    lines = []
    for item in result.get("by_date") or []:
        cells = [
            f'<td class="l">{html.escape(str(item["date"]))}</td>',
            f'<td class="l">{html.escape(str(item["tier_label"]))}</td>',
            f'<td>{item["picks"]}</td>',
        ]
        for horizon in horizons:
            cells.append(f'<td>{_pct((item.get("avg") or {}).get(str(horizon)))}</td>')
            cells.append(f'<td>{_pct((item.get("excess") or {}).get(str(horizon)))}</td>')
        status_cls = "muted" if item.get("status") != "已完成" else ""
        cells.append(f'<td class="{status_cls}">{html.escape(str(item.get("status") or ""))}</td>')
        lines.append("<tr>" + "".join(cells) + "</tr>")
    by_date_table = f'<div class="wrap"><table>{head}{"".join(lines)}</table></div>'

    # ③ 个股明细
    head = (
        '<tr><th class="l">名单日</th><th class="l">梯队</th><th class="l">代码</th>'
        '<th class="l">名称</th><th class="l">行业</th><th>买入价</th>'
        + "".join(f"<th>{h}日</th>" for h in horizons)
        + "<th>超额(1日)</th><th>期间最大涨</th><th>期间最大回撤</th><th>状态</th></tr>"
    )
    lines = []
    for row in rows:
        code = html.escape(str(row.get("code") or ""))
        secid = str(row.get("secid") or "")
        url = xueqiu_url(secid) if secid else ""
        link = f'<a href="{html.escape(url)}" target="_blank" rel="noopener">{code}</a>'
        cells = [
            f'<td class="l">{html.escape(str(row.get("date") or ""))}</td>',
            f'<td class="l">{html.escape(str(row.get("tier_label") or ""))}</td>',
            f'<td class="l">{link}</td>',
            f'<td class="l">{html.escape(str(row.get("name") or ""))}</td>',
            f'<td class="l">{html.escape(str(row.get("industry") or ""))}</td>',
            "<td>" + ("-" if row.get("entry") is None else f"{float(row['entry']):.2f}") + "</td>",
        ]
        for horizon in horizons:
            cells.append(f'<td>{_pct((row.get("rets") or {}).get(horizon))}</td>')
        excess = (row.get("excess") or {}).get(horizons[0] if horizons else 1)
        cells.append(f"<td>{_pct(excess)}</td>")
        cells.append(f'<td>{_pct(row.get("max_gain"), 1)}</td>')
        cells.append(f'<td>{_pct(row.get("max_drawdown"), 1)}</td>')
        status = str(row.get("status") or "")
        cells.append(
            f'<td class="{"muted" if status != "已完成" else ""}">{html.escape(status)}</td>'
        )
        lines.append("<tr>" + "".join(cells) + "</tr>")
    detail_table = f'<div class="wrap"><table>{head}{"".join(lines)}</table></div>'

    unmatched = ""
    if result.get("unmatched"):
        items = "、".join(
            f'{html.escape(str(it.get("date")))} 第{it.get("tier")}梯队 '
            f'{html.escape(str(it.get("code") or it.get("name")))}'
            for it in result["unmatched"][:12]
        )
        unmatched = (
            '<div class="banner">有几条名单对不上本地股票（AI 写错代码/名称，或已退市），已丢弃：'
            f"{items}</div>"
        )

    body = (
        hint
        + f"<p class='hint'>共 {len(rows)} 条跟踪记录 · "
        + f'{len(result.get("by_date") or [])} 个（名单日 × 梯队）</p>'
        + unmatched
        + "<h2>① 按梯队汇总（累计所有名单日）</h2>"
        + by_tier_table
        + "<h2>② 按名单日</h2>"
        + by_date_table
        + "<h2>③ 个股明细</h2>"
        + detail_table
    )
    return _tiers_page_shell(title, toolbar, body, "")


def _tiers_page_shell(title: str, toolbar: str, body: str, _extra: str = "") -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>梯队回测 · {html.escape(title)}</title><style>{TIERS_CSS}</style></head><body>"
        f"<header><h1>梯队回测（复盘第一/第二梯队跟踪）</h1>"
        f'<div class="hint">数据来自每份复盘文末的 TIER1/TIER2 名单 + 本地日线行情</div></header>'
        f"{toolbar}<main>{body}</main>"
        "<footer>代码点击跳转雪球行情 · 收益均按交易日计算 · "
        "「跟踪中」表示持有期未走完，数字会随后续行情更新</footer>"
        "</body></html>"
    )


def _render_pager(active_size: int) -> str:
    """分页条：每页行数下拉 + 上一页/下一页。"""
    options = "".join(
        '<option value="%d"%s>%d</option>'
        % (size, " selected" if size == active_size else "", size)
        for size in PAGE_SIZES
    )
    return (
        '<div class="pager">'
        '<span class="pg-info">共 <b class="pg-total">-</b> 条，当前 <b class="pg-range">-</b></span>'
        '<label class="pg-sizes">每页 <select id="pageSize">' + options + "</select> 行</label>"
        '<span class="pg-nav">'
        '<a class="pg-prev" data-page="prev">上一页</a>'
        '<span class="pg-page">第 1 / 1 页</span>'
        '<a class="pg-next" data-page="next">下一页</a>'
        "</span>"
        "</div>"
    )


def _render_board_filter(groups) -> str:
    """板块多选下拉（默认全选）。"""
    if len(groups) <= 1:
        return ""
    items = "".join(
        '<label><input type="checkbox" value="%s" checked>%s</label>'
        % (html.escape(group), html.escape(group))
        for group in groups
    )
    return (
        '<details class="dd"><summary id="boardSummary">板块：全部</summary>'
        f'<div class="dd-body">{items}</div></details>'
    )


# --------------------------------------------------------------------------- 服务
def make_handler(
    store_factory: Callable[[], Store],
    title: str = "A股日频信号",
    limit: int = 300,
    password: str = "",
    session_days: int = DEFAULT_SESSION_DAYS,
    remember_sort: bool = True,
    update_times: str = DEFAULT_UPDATE_TIMES,
    page_size: int = DEFAULT_PAGE_SIZE,
    vol_up_min_pct: float = 3.0,
    view_help: Optional[Dict[str, str]] = None,
    summary_after_cfg: str = "17:00",
    backtest_cfg: Any = None,
) -> type:
    """生成一个 ``BaseHTTPRequestHandler`` 子类。

    路由：

    * ``/``                          -> 默认视图（放量）
    * ``/signals/<view>``            -> 四个主信号之一，支持 ``date/sort/order/q``
    * ``/api/signals?view=...``      -> JSON（给以后做小程序/微信/看板用）
    * ``/login`` ``/logout``         -> 口令登录 / 退出（Cookie 会话，默认 7 天）
    * ``/healthz``                   -> 探活（**不校验口令**，只回 ``ok``、不含任何数据，
      方便 nginx/k8s 的 healthCheck 直接打）

    页面上的排序是纯前端的（点表头立刻重排，地址栏始终只有视图名）；
    ``remember_sort`` 打开时用 localStorage 记住上次排序列。

    页面**只显示最新一天的缓存**（不提供翻历史），并显著标注"数据日期 / 是不是当天 /
    系统几点更新"，``update_times`` 用来生成那段提示。

    鉴权：设置了口令就要求登录（Cookie 会话），另外支持 HTTP Basic
    （``curl -u ":$口令"``）方便脚本/接口调用。口令为空则视为"开放访问"（只适合本机）。
    """
    cfg_for_backtest = backtest_cfg if backtest_cfg is not None else _NullCfg()
    try:
        default_backtest_days = int(cfg_for_backtest.get("backtest.days", 90) or 90)
    except (TypeError, ValueError):  # pragma: no cover - 配置写坏了不该影响网页
        default_backtest_days = 90

    class Handler(BaseHTTPRequestHandler):
        server_version = "emscan-web"
        # HTTP/1.1 = 长连接（手机上一次握手看多页），我们自己保证 Content-Length 正确
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # 收敛日志，别刷屏
            log.debug("%s - %s", self.address_string(), fmt % args)

        # ------------------------------------------------------------- 工具
        def _params(self, parsed) -> Dict[str, str]:
            raw = parse_qs(parsed.query)
            return {key: values[0] for key, values in raw.items() if values}

        def _cookie(self, name: str) -> str:
            raw = self.headers.get("Cookie", "")
            for part in raw.split(";"):
                key, _, value = part.strip().partition("=")
                if key == name:
                    return value
            return ""

        def _session_ok(self) -> bool:
            if not password:
                return True
            return check_session(password, self._cookie(SESSION_COOKIE))

        def _basic_ok(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.lower().startswith("basic "):
                return False
            try:
                decoded = base64.b64decode(header.split(" ", 1)[1].strip()).decode("utf-8")
            except Exception:
                return False
            _, _, supplied = decoded.partition(":")
            return check_password(password, supplied)

        def _authed(self) -> bool:
            return self._session_ok() or self._basic_ok()

        def _secure_cookie(self) -> bool:
            return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

        def _set_session(self, value: str, days: int) -> None:
            attrs = [
                f"{SESSION_COOKIE}={value}",
                "Path=/",
                "HttpOnly",
                "SameSite=Lax",
            ]
            if days > 0:
                attrs.append(f"Max-Age={int(days) * 86400}")
            if self._secure_cookie():
                attrs.append("Secure")
            self.send_header("Set-Cookie", "; ".join(attrs))

        def _redirect(self, location: str, cookie: Optional[Tuple[str, int]] = None) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:
                self._set_session(cookie[0], cookie[1])
            self.end_headers()

        def _read_form(self, limit: int = 8192) -> Dict[str, str]:
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            body = self.rfile.read(min(length, limit)).decode("utf-8", "replace")
            raw = parse_qs(body)
            return {key: values[0] for key, values in raw.items() if values}

        def _send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, 6)  # 页面 25~50KB，压完通常 1/4 大小
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server 的命名约定
            parsed = urlparse(self.path)
            params = self._params(parsed)
            path = parsed.path.rstrip("/") or "/"
            if path == "/healthz":
                self._send(b"ok", ctype="text/plain; charset=utf-8")
                return
            if path == "/login":
                if self._session_ok():
                    self._redirect("/")
                    return
                body = render_login(title, next_path=_safe_next(params.get("next")))
                self._send(body.encode("utf-8"))
                return
            if path == "/logout":
                self._redirect("/login", cookie=("", 0))
                return

            if not self._authed():
                if path.startswith("/api/"):
                    self._send(
                        json.dumps({"error": "unauthorized"}, ensure_ascii=False).encode("utf-8"),
                        401,
                        "application/json; charset=utf-8",
                    )
                else:
                    target = quote(_safe_next(self.path), safe="/?&=%")
                    self._redirect(f"/login?next={target}")
                return

            if path in ("/tiers", "/tiers.csv", "/api/tiers"):
                days_value = params.get("days")
                try:
                    days = (
                        int(days_value) if days_value not in (None, "") else default_backtest_days
                    )
                except ValueError:
                    days = default_backtest_days
                try:
                    st = store_factory()
                    result = tiers_report(st, cfg_for_backtest, days=days or None)
                except Exception as exc:  # 页面永远要能打开
                    log.exception("梯队回测渲染失败：%s", exc)
                    self._send(
                        f"服务端错误：{exc}".encode("utf-8"), 500, "text/plain; charset=utf-8"
                    )
                    return
                if path == "/api/tiers":
                    payload = json.dumps(result, ensure_ascii=False, indent=2)
                    self._send(payload.encode("utf-8"), 200, "application/json; charset=utf-8")
                    return
                if path == "/tiers.csv":
                    import csv
                    import io

                    buffer = io.StringIO()
                    horizon_keys = [
                        key
                        for horizon in result.get("horizons") or []
                        for key in (f"ret_{horizon}d", f"excess_{horizon}d")
                    ]
                    columns = [
                        "date",
                        "tier_label",
                        "code",
                        "name",
                        "industry",
                        "entry_date",
                        "entry",
                        "close_t",
                        "status",
                    ] + horizon_keys
                    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
                    writer.writeheader()
                    for row in result.get("rows") or []:
                        record = {key: row.get(key) for key in columns}
                        for horizon in result.get("horizons") or []:
                            record[f"ret_{horizon}d"] = (row.get("rets") or {}).get(horizon)
                            record[f"excess_{horizon}d"] = (row.get("excess") or {}).get(horizon)
                        writer.writerow(record)
                    raw = buffer.getvalue().encode("utf-8-sig")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header(
                        "Content-Disposition", 'attachment; filename="tiers_backtest.csv"'
                    )
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                page = render_tiers_page(
                    result,
                    title=title,
                    days=days,
                    entry=tiers_entry_mode(cfg_for_backtest),
                )
                self._send(page.encode("utf-8"))
                return

            if path in ("/summary", "/summary.md"):
                st = store_factory()
                date = str(params.get("date") or "") or (st.latest_summary_date() or "")
                record = st.get_summary(date) if date else None
                if not record:
                    body = (
                        "当日总结还没生成（默认 %s 之后那次更新时生成，数据更完整）。"
                        % summary_after_cfg
                    ).encode("utf-8")
                    if path.endswith(".md"):
                        self._send(body, 404, "text/plain; charset=utf-8")
                    else:
                        self._send(body, 200, "text/plain; charset=utf-8")
                    return
                content = str(record.get("content") or "")
                if path.endswith(".md"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        'attachment; filename="summary_%s.md"' % str(record.get("date")),
                    )
                    raw = content.encode("utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                page = (
                    '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    "<title>%s 当日总结</title><style>body{background:#0f1115;color:#e8ecf3;"
                    "font:14px/1.7 -apple-system,'PingFang SC',Arial,sans-serif;"
                    "margin:0;padding:14px}"
                    "pre{white-space:pre-wrap;word-break:break-word;background:#171a21;"
                    "border:1px solid "
                    "#262b36;border-radius:10px;padding:12px}a{color:#4aa8ff}</style></head><body>"
                    '<p><a href="/">← 返回</a> · <a href="/summary.md">下载 Markdown</a> · '
                    '<a href="/tiers">梯队回测</a> · 引擎 %s</p>'
                    "<pre>%s</pre></body></html>"
                ) % (
                    str(record.get("date")),
                    str(record.get("engine")),
                    html.escape(content),
                )
                self._send(page.encode("utf-8"))
                return

            view = params.get("view")
            if path.startswith("/signals/"):
                view = path.split("/signals/", 1)[1] or view
            elif path not in ("/", "/index.html", "/api/signals"):
                self._send("404 Not Found".encode("utf-8"), 404, "text/plain; charset=utf-8")
                return

            is_api = path == "/api/signals"
            try:
                ctx = build_context(
                    store_factory(),
                    view=view,
                    date=params.get("date"),
                    sort=params.get("sort"),
                    order=params.get("order"),
                    query=params.get("q", ""),
                    limit=int(params.get("limit", limit) or limit),
                    latest_only=not is_api,  # 页面只看最新一天；API 允许指定日期/排序
                    update_times=update_times,
                    vol_up_min_pct=vol_up_min_pct,
                )
            except Exception as exc:  # 页面永远要能打开
                log.exception("网页渲染失败：%s", exc)
                self._send(f"服务端错误：{exc}".encode("utf-8"), 500, "text/plain; charset=utf-8")
                return

            if is_api:
                self._send(
                    render_json(ctx).encode("utf-8"), ctype="application/json; charset=utf-8"
                )
            else:
                self._send(
                    render_html(
                        ctx,
                        title=title,
                        show_logout=bool(password),
                        remember_sort=remember_sort,
                        page_size=page_size,
                        help_text=(view_help or {}).get(normalize_view(view), ""),
                        help_map=view_help or {},
                    ).encode("utf-8")
                )

        def do_POST(self) -> None:  # noqa: N802 - http.server 的命名约定
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path != "/login":
                self._send(
                    "405 Method Not Allowed".encode("utf-8"), 405, "text/plain; charset=utf-8"
                )
                return
            form = self._read_form()
            next_path = _safe_next(form.get("next"))
            ip = self.client_address[0] if self.client_address else "?"
            if password and _login_failures(ip) >= LOGIN_MAX_FAILURES:
                self._send(
                    render_login(
                        title,
                        next_path=next_path,
                        error="失败次数过多，请等几分钟再试",
                    ).encode("utf-8"),
                    429,
                )
                return
            if password and check_password(password, form.get("password", "")):
                _clear_login_failures(ip)
                days = session_days if form.get("remember") else 1
                self._redirect(next_path, cookie=(issue_session(password, days), days))
                return
            if password:
                _record_login_failure(ip)
            self._send(
                render_login(title, next_path=next_path, error="口令不对，请重试").encode("utf-8")
            )

    return Handler


def serve(
    store_factory: Callable[[], Store],
    host: str = "127.0.0.1",
    port: int = 8080,
    title: str = "A股日频信号",
    limit: int = 300,
    password: str = "",
    session_days: int = DEFAULT_SESSION_DAYS,
    remember_sort: bool = True,
    update_times: str = DEFAULT_UPDATE_TIMES,
    page_size: int = DEFAULT_PAGE_SIZE,
    vol_up_min_pct: float = 3.0,
    view_help: Optional[Dict[str, str]] = None,
    summary_after_cfg: str = "17:00",
    backtest_cfg: Any = None,
    blocking: bool = True,
) -> ThreadingHTTPServer:
    """起一个网页服务，返回 server 对象（``blocking=False`` 时自己开线程，方便测试）。"""
    handler = make_handler(
        store_factory,
        title=title,
        limit=limit,
        password=password,
        session_days=session_days,
        remember_sort=remember_sort,
        update_times=update_times,
        page_size=page_size,
        vol_up_min_pct=vol_up_min_pct,
        view_help=view_help,
        summary_after_cfg=summary_after_cfg,
        backtest_cfg=backtest_cfg,
    )
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    log.info(
        "网页端已启动：http://%s:%s/ （四个主信号：%s）",
        host,
        httpd.server_port,
        "、".join(spec["nav"] for spec in VIEWS.values()),
    )
    log.info("页面只显示最新一天缓存；%s", format_update_note(update_times))
    if password:
        log.info("已开启口令登录：浏览器首次访问跳 /login，登录后 Cookie 记住 %s 天", session_days)
    elif host not in ("127.0.0.1", "localhost"):
        log.warning(
            "监听 %s 但没设置口令（web.password / EMSCAN_WEB_PASSWORD 为空）："
            "任何人都能看数据，请设置口令或用反向代理加认证/TLS",
            host,
        )
    else:
        log.info("未设置口令：只监听本机，视为开放访问（对外暴露请加 --password）")
    if blocking:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - 手动 Ctrl+C
            log.info("网页端已停止")
        finally:
            httpd.server_close()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
