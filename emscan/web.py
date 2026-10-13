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

import html
import gzip
import json
import logging
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

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


def render_html(ctx: PageContext, title: str = "A股日频信号") -> str:
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
  <div class="brand">{html.escape(title)}<span>{html.escape(ctx.title or "")}</span></div>
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
    token: str = "",
) -> type:
    """生成一个 ``BaseHTTPRequestHandler`` 子类。

    路由：

    * ``/``                          -> 默认视图（放量）
    * ``/signals/<view>``            -> 四个主信号之一，支持 ``date/sort/order/q``
    * ``/api/signals?view=...``      -> JSON（给以后做小程序/微信/看板用）
    * ``/healthz``                   -> 探活（**不校验口令**，只回 ``ok``、不含任何数据，
      方便 nginx/k8s 的 healthCheck 直接打；别的路径都要口令）
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

        def _authorized(self, params: Dict[str, str]) -> bool:
            if not token:
                return True
            supplied = params.get("token") or self.headers.get("X-Emscan-Token") or ""
            return supplied == token

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
            if not self._authorized(params):
                self._send(
                    "需要 token：在后面加 ?token=xxx".encode("utf-8"),
                    401,
                    "text/plain; charset=utf-8",
                )
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
                self._send(render_html(ctx, title=title).encode("utf-8"))

    return Handler


def serve(
    store_factory: Callable[[], Store],
    host: str = "127.0.0.1",
    port: int = 8080,
    title: str = "A股日频信号",
    limit: int = 300,
    token: str = "",
    blocking: bool = True,
) -> ThreadingHTTPServer:
    """起一个网页服务，返回 server 对象（``blocking=False`` 时自己开线程，方便测试）。"""
    handler = make_handler(store_factory, title=title, limit=limit, token=token)
    httpd = ThreadingHTTPServer((host, int(port)), handler)
    log.info(
        "网页端已启动：http://%s:%s/ （四个主信号：%s）",
        host,
        httpd.server_port,
        "、".join(spec["nav"] for spec in VIEWS.values()),
    )
    if token:
        log.info("已开启简单口令校验：访问时带 ?token=xxx（或请求头 X-Emscan-Token）")
    if host not in ("127.0.0.1", "localhost") and not token:
        log.warning(
            "监听 %s 且没有设置 token：请确保只在可信网络里暴露，或用反向代理加认证/TLS", host
        )
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
