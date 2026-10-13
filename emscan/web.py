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
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlparse

from .mairui import BOARD_LABELS
from .signals import FLAG_LABELS
from .store import Store

log = logging.getLogger("emscan")

# 四个主信号（导航栏就是这四个）
# key -> 标记列 / 导航标题 / 页面标题 / 默认排序字段 / 默认方向
VIEWS: Dict[str, Dict[str, str]] = {
    "vol_spike": {
        "flag": "s_vol_spike",
        "nav": "放量",
        "title": "突然放量",
        "sort": "amount_yi",
        "order": "desc",
    },
    "macd_golden": {
        "flag": "s_macd_golden",
        "nav": "MACD金叉",
        "title": "MACD 金叉（DIFF 上穿 DEA）",
        "sort": "amount_yi",
        "order": "desc",
    },
    "kdj_golden": {
        "flag": "s_kdj_golden",
        "nav": "KDJ金叉",
        "title": "KDJ 金叉（K 上穿 D）",
        "sort": "amount_yi",
        "order": "desc",
    },
    "boll_break_lower": {
        "flag": "s_boll_break_lower",
        "nav": "布林下轨",
        "title": "BOLL 下轨突破（收盘 ≤ 下轨）",
        "sort": "boll_pos",
        "order": "asc",
    },
}

DEFAULT_VIEW = "vol_spike"

# 表格列：(字段, 表头, 数值格式, 可排序)
COLUMNS: Tuple[Tuple[str, str, Optional[str], bool], ...] = (
    ("code", "代码", None, True),
    ("name", "名称", None, True),
    ("board", "板块", None, True),
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
    ("signals_cn", "命中信号", None, False),
)

SORTABLE: Tuple[str, ...] = tuple(key for key, _, _, sortable in COLUMNS if sortable)

MARKET_PREFIX = {1: "SH", 0: "SZ", 2: "BJ"}

# 展示列 -> 缓存表里真正用来排序的字段（成交额展示单位是亿，表里存的是元）
SORT_COLUMN_SQL = {"amount_yi": "d.amount"}

# 页面内容只取决于"缓存里有什么"，同一天里反复看同一页不必重算：
# 键 = (数据版本, 查询参数)，数据一变（computed_at 变了）自动失效。
_CACHE: Dict[Tuple[Any, ...], "PageContext"] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 128

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

    def as_dict(self) -> Dict[str, Any]:
        return {
            "view": self.view,
            "date": self.date,
            "sort": self.sort,
            "order": self.order,
            "query": self.query,
            "total": self.total,
            "dates": self.dates,
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
) -> PageContext:
    """从缓存里取一页数据：按"日期 + 信号"筛，再搜索、排序、截断。

    全程用 sqlite3 + 纯 Python（不经过 pandas），再套一层"数据版本"缓存：
    单页渲染从 ~26 ms 降到冷态 ~7 ms、暖态 ~0.2 ms（见 README"网页端"一节）。
    """
    view_key = normalize_view(view)
    spec = VIEWS[view_key]
    sort_key, sort_order = normalize_sort(sort, order, view_key)
    query = str(query or "").strip()
    limit = max(1, int(limit))

    version = _data_version(store) if use_cache else None
    cache_key = (version, view_key, date, sort_key, sort_order, query, limit)
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
        date=date,
        sort=sort_key,
        order=sort_order,
        query=query,
        dates=dates,
        title=spec["title"],
        limit=limit,
    )
    if not dates:
        ctx.error = (
            "还没有每日信号缓存：先在服务器上跑一次 `python3 -m emscan run`（或 scan + signals）"
        )
        return ctx
    if ctx.date not in dates:
        ctx.date = dates[0]

    where = ["d.date = ?", "COALESCE(d.%s, 0) = 1" % spec["flag"]]
    params: List[Any] = [ctx.date]
    if ctx.query:
        like = "%" + ctx.query.upper() + "%"
        where.append("(UPPER(IFNULL(s.code, '')) LIKE ? OR UPPER(IFNULL(s.name, '')) LIKE ?)")
        params.extend([like, like])
    clause = " AND ".join(where)
    source = "FROM daily_signals_named d LEFT JOIN symbols s ON s.secid = d.secid WHERE " + clause

    ctx.total = int(
        store.conn.execute("SELECT COUNT(*) AS n " + source, params).fetchone()["n"] or 0
    )
    if ctx.total:
        sort_sql = SORT_COLUMN_SQL.get(ctx.sort, "d." + ctx.sort)
        direction = "ASC" if ctx.order == "asc" else "DESC"
        rows = store.conn.execute(
            "SELECT d.*, s.code AS code, s.name AS name, s.board AS board "
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
    item: Dict[str, Any] = {
        "secid": secid,
        "code": str(row["code"] or secid.split(".")[-1]),
        "name": str(row["name"] or ""),
        "board": BOARD_LABELS.get(str(row["board"]), str(row["board"] or "")),
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
header{position:sticky;top:0;z-index:5;background:rgba(15,17,21,.96);
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
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
thead th{position:sticky;top:0;background:var(--card);text-align:right;
white-space:nowrap;padding:8px;border-bottom:1px solid var(--line);
font-size:12px;color:var(--muted)}
thead th.left{text-align:left}
thead th a{color:var(--fg)}
thead th a.active{color:var(--accent);font-weight:700}
tbody td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
tbody td.left{text-align:left}
tr:hover td{background:#1b1f28}
.pos{color:var(--up)}
.neg{color:var(--down)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:10px 12px;margin-bottom:8px}
.card h3{margin:0 0 6px;font-size:15px}
.card .kv{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 10px;font-size:13px}
.card .kv div{display:flex;justify-content:space-between;gap:8px;
border-bottom:1px dashed #232833;padding:2px 0}
.card .kv span:first-child{color:var(--muted)}
.cards{display:none}
@media (max-width:760px){.wrap{display:none}.cards{display:block}}
footer{color:var(--muted);font-size:11px;padding:0 12px 24px}
"""

JS = """\
document.querySelectorAll('th a[data-sort]').forEach(function(link){
  link.addEventListener('click',function(ev){
    var table=link.closest('table');
    if(!table||!table.tBodies[0])return;
    ev.preventDefault();
    var body=table.tBodies[0];
    var rows=Array.prototype.slice.call(body.rows);
    var index=link.closest('th').cellIndex;
    var dir=(link.dataset.order==='asc')?1:-1;
    function num(row){
      var text=(row.cells[index]?row.cells[index].textContent:'').trim();
      var value=parseFloat(text.replace(/[+%,]/g,''));
      return isNaN(value)?null:value;
    }
    rows.sort(function(a,b){
      var x=num(a),y=num(b);
      if(x!==null&&y!==null)return (x-y)*dir;
      var xs=(x===null?'':x),ys=(y===null?'':y);
      if(typeof xs!=='number'||typeof ys!=='number'){
        return String(xs).localeCompare(String(ys),'zh')*dir;
      }
      return 0;
    });
    rows.forEach(function(row){body.appendChild(row)});
    link.dataset.order=(link.dataset.order==='asc')?'desc':'asc';
  });
});
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


def render_html(ctx: PageContext, title: str = "A股日频信号", show_logout: bool = False) -> str:
    """把一页渲染成完整 HTML（服务端渲染，手机/桌面同一份）。"""
    nav = []
    for key, spec in VIEWS.items():
        active = ' class="active"' if key == ctx.view else ""
        nav.append(
            f'<a{active} href="/signals/{key}?date={html.escape(ctx.date or "")}'
            f'&sort={html.escape(ctx.sort)}&order={html.escape(ctx.order)}">'
            f'{html.escape(spec["nav"])}</a>'
        )

    head_cells = []
    for key, label, _, sortable in COLUMNS:
        cls = "left" if key in ("code", "name", "board", "signals_cn") else ""
        if sortable:
            nxt = "asc" if not (ctx.sort == key and ctx.order == "desc") else "desc"
            active = ' class="active"' if ctx.sort == key else ""
            arrow = ("↓" if ctx.order == "desc" else "↑") if ctx.sort == key else "↕"
            cell = (
                f'<th class="{cls}"><a{active} data-sort="{key}" data-order="{nxt}" '
                f'href="/signals/{ctx.view}?date={html.escape(ctx.date or "")}&sort={key}'
                f'&order={nxt}">{html.escape(label)} {arrow}</a></th>'
            )
        else:
            cell = f'<th class="{cls}">{html.escape(label)}</th>'
        head_cells.append(cell)

    body_rows = []
    card_rows = []
    for row in ctx.rows:
        cells = []
        for key, label, fmt, _ in COLUMNS:
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
        body_rows.append("<tr>" + "".join(cells) + "</tr>")
        kv = "".join(
            f"<div><span>{html.escape(label)}</span><span>{_fmt(row.get(key), fmt)}</span></div>"
            for key, label, fmt, _ in COLUMNS
            if key not in ("code", "name")
        )
        card_rows.append(
            f'<div class="card"><h3><a href="{html.escape(row["url"])}" target="_blank" '
            f'rel="noopener">{html.escape(row["code"])} {html.escape(row["name"])}</a></h3>'
            f'<div class="kv">{kv}</div></div>'
        )

    order_cn = "升序" if ctx.order == "asc" else "降序"
    sort_label = dict((key, label) for key, label, _, _ in COLUMNS).get(ctx.sort, ctx.sort)
    hint = (
        f"交易日 <b>{html.escape(ctx.date or '-')}</b> · 命中 <b>{ctx.total}</b> 只"
        f"（显示前 {min(ctx.total, ctx.limit)} 只）· 排序：<b>{html.escape(sort_label)} {order_cn}</b>"
    )
    if ctx.query:
        hint += f" · 搜索：<b>{html.escape(ctx.query)}</b>"
    dates = "".join(
        f'<option value="{html.escape(day)}"'
        f'{" selected" if day == ctx.date else ""}>{html.escape(day)}</option>'
        for day in ctx.dates[:120]
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
                + f'<div class="cards">{"".join(card_rows)}</div>'
            )
        )
    )
    empty = "" if ctx.error or ctx.rows else '<p class="hint">这一天没有命中该信号。</p>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} · {html.escape(ctx.title or "")}</title>
<style>{CSS}</style></head>
<body>
<header>
  <div class="brand">{html.escape(title)}<span>{html.escape(ctx.title or "")}</span>
    {('<a class="logout" href="/logout">退出登录</a>') if show_logout else ""}</div>
  <nav>{"".join(nav)}</nav>
  <form class="toolbar" method="get" action="/signals/{ctx.view}">
    <label>日期 <select name="date" onchange="this.form.submit()">{dates}</select></label>
    <input name="q" value="{html.escape(ctx.query)}" placeholder="代码/名称" size="10">
    <input type="hidden" name="sort" value="{html.escape(ctx.sort)}">
    <input type="hidden" name="order" value="{html.escape(ctx.order)}">
    <button type="submit">查询</button>
  </form>
</header>
<main>
  <div class="hint">{hint}</div>
  {body}{empty}
</main>
<footer>数据来自本地每日信号缓存（daily_signals）· 代码点击跳转雪球行情 · 表头可点击排序</footer>
<script>{JS}</script>
</body></html>
"""


def render_json(ctx: PageContext) -> str:
    return json.dumps(ctx.as_dict(), ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- 服务
def make_handler(
    store_factory: Callable[[], Store],
    title: str = "A股日频信号",
    limit: int = 300,
    password: str = "",
    session_days: int = DEFAULT_SESSION_DAYS,
) -> type:
    """生成一个 ``BaseHTTPRequestHandler`` 子类。

    路由：

    * ``/``                          -> 默认视图（放量）
    * ``/signals/<view>``            -> 四个主信号之一，支持 ``date/sort/order/q``
    * ``/api/signals?view=...``      -> JSON（给以后做小程序/微信/看板用）
    * ``/login`` ``/logout``         -> 口令登录 / 退出（Cookie 会话，默认 7 天）
    * ``/healthz``                   -> 探活（**不校验口令**，只回 ``ok``、不含任何数据，
      方便 nginx/k8s 的 healthCheck 直接打）

    鉴权：设置了口令就要求登录（Cookie 会话），另外支持 HTTP Basic
    （``curl -u ":$口令"``）方便脚本/接口调用。口令为空则视为"开放访问"（只适合本机）。
    """

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

            view = params.get("view")
            if path.startswith("/signals/"):
                view = path.split("/signals/", 1)[1] or view
            elif path not in ("/", "/index.html", "/api/signals"):
                self._send("404 Not Found".encode("utf-8"), 404, "text/plain; charset=utf-8")
                return

            try:
                ctx = build_context(
                    store_factory(),
                    view=view,
                    date=params.get("date"),
                    sort=params.get("sort"),
                    order=params.get("order"),
                    query=params.get("q", ""),
                    limit=int(params.get("limit", limit) or limit),
                )
            except Exception as exc:  # 页面永远要能打开
                log.exception("网页渲染失败：%s", exc)
                self._send(f"服务端错误：{exc}".encode("utf-8"), 500, "text/plain; charset=utf-8")
                return

            if path == "/api/signals":
                self._send(
                    render_json(ctx).encode("utf-8"), ctype="application/json; charset=utf-8"
                )
            else:
                self._send(
                    render_html(ctx, title=title, show_logout=bool(password)).encode("utf-8")
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
    blocking: bool = True,
) -> ThreadingHTTPServer:
    """起一个网页服务，返回 server 对象（``blocking=False`` 时自己开线程，方便测试）。"""
    handler = make_handler(
        store_factory, title=title, limit=limit, password=password, session_days=session_days
    )
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    log.info(
        "网页端已启动：http://%s:%s/ （四个主信号：%s）",
        host,
        httpd.server_port,
        "、".join(spec["nav"] for spec in VIEWS.values()),
    )
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
