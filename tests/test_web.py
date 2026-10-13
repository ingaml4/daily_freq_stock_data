"""网页端测试：导航/四个视图、排序、搜索、雪球链接、HTML 转义、HTTP 服务。"""

import base64
import http.cookiejar
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from helpers import make_cfg

from emscan.cli import open_store
from emscan.mairui import BOARD_LABELS
from emscan.web import (
    DEFAULT_VIEW,
    VIEWS,
    _safe_next,
    build_context,
    check_password,
    check_session,
    clear_cache,
    flag_names,
    issue_session,
    normalize_sort,
    render_html,
    serve,
    xueqiu_url,
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """测试里需要看到 303 本身，而不是被自动跟随。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _seed(tmp_path, config=None):
    cfg = make_cfg(tmp_path, config or {})
    store = open_store(cfg)
    store.upsert_symbols(
        [
            {
                "secid": "1.600000",
                "code": "600000",
                "name": "浦发银行",
                "market": 1,
                "board": "sh_main",
            },
            {
                "secid": "1.600001",
                "code": "600001",
                "name": "邯郸钢铁",
                "market": 1,
                "board": "sh_main",
            },
            {
                "secid": "0.300001",
                "code": "300001",
                "name": "特锐德",
                "market": 0,
                "board": "sz_gem",
            },
            {
                "secid": "0.000933",
                "code": "000933",
                "name": "神火股份",
                "market": 0,
                "board": "sz_main",
            },
            {
                "secid": "1.601058",
                "code": "601058",
                "name": "赛轮轮胎",
                "market": 1,
                "board": "sh_main",
            },
            {
                "secid": "0.300442",
                "code": "300442",
                "name": "<b>润泽科技</b>",
                "market": 0,
                "board": "sz_gem",
            },
            {
                "secid": "1.600519",
                "code": "600519",
                "name": "贵州茅台",
                "market": 1,
                "board": "sh_main",
            },
        ]
    )
    date = "2026-10-13"
    store.upsert_daily_signals(
        [
            {
                "secid": "0.000933",
                "date": date,
                "close": 12.0,
                "pct_chg": -3.5,
                "amount": 2.0e8,
                "turnover": 5.5,
                "vol_ratio_5": 9.9,
                "vol_ratio_20": 4.4,
                "vol_z60": 8.1,
                "boll_pos": 0.2,
                "boll_lower": 11.0,
                "macd_diff": 0.1,
                "macd_dea": 0.05,
                "kdj_k": 30.0,
                "kdj_d": 25.0,
                "s_vol_spike": 1,
                "s_macd_golden": 1,
            },
            {
                "secid": "1.601058",
                "date": date,
                "close": 8.8,
                "pct_chg": 6.2,
                "amount": 9.0e8,
                "turnover": 2.0,
                "vol_ratio_5": 3.0,
                "vol_ratio_20": 2.5,
                "boll_pos": 0.9,
                "s_kdj_golden": 1,
                "s_macd_golden": 1,
            },
            {
                "secid": "0.300442",
                "date": date,
                "close": 30.0,
                "pct_chg": 1.0,
                "amount": 5.0e8,
                "turnover": 1.0,
                "boll_pos": -0.05,
                "s_boll_break_lower": 1,
            },
            {
                "secid": "1.600519",
                "date": date,
                "close": 1500.0,
                "pct_chg": 0.5,
                "amount": 3.0e9,
                "turnover": 0.3,
            },
            # 再补几只"放量命中"的样本，让每一列都有多个不同值（排序测试才不退化）
            {
                "secid": "1.600000",
                "date": date,
                "close": 5.5,
                "pct_chg": -1.2,
                "amount": 3.0e8,
                "turnover": 8.8,
                "vol_ratio_5": 2.2,
                "vol_ratio_20": 1.9,
                "vol_z60": 3.3,
                "boll_pos": 0.55,
                "boll_lower": 5.0,
                "macd_diff": -0.5,
                "macd_dea": -0.2,
                "macd_bar": -0.6,
                "kdj_k": 20.0,
                "kdj_d": 30.0,
                "kdj_j": 0.0,
                "s_vol_spike": 1,
            },
            {
                "secid": "1.600001",
                "date": date,
                "close": 88.8,
                "pct_chg": 9.9,
                "amount": 1.2e9,
                "turnover": 15.5,
                "vol_ratio_5": 4.4,
                "vol_ratio_20": 3.3,
                "vol_z60": 6.6,
                "boll_pos": -0.1,
                "boll_lower": 90.0,
                "macd_diff": 1.1,
                "macd_dea": 0.4,
                "macd_bar": 1.4,
                "kdj_k": 80.0,
                "kdj_d": 60.0,
                "kdj_j": 95.0,
                "s_vol_spike": 1,
            },
            {
                "secid": "0.300001",
                "date": date,
                "close": 12.3,
                "pct_chg": 0.9,
                "amount": 6.0e7,
                "turnover": 3.3,
                "vol_ratio_5": 2.9,
                "vol_ratio_20": 2.1,
                "vol_z60": 4.1,
                "boll_pos": 0.33,
                "boll_lower": 11.9,
                "macd_diff": 0.2,
                "macd_dea": 0.1,
                "macd_bar": 0.2,
                "kdj_k": 55.0,
                "kdj_d": 50.0,
                "kdj_j": 65.0,
                "s_vol_spike": 1,
            },
            {
                "secid": "1.601058",
                "date": "2026-10-10",
                "close": 8.2,
                "pct_chg": 1.1,
                "amount": 1.0e8,
                "s_vol_spike": 1,
            },
        ]
    )
    return cfg, store, date


def test_session_and_password_helpers():
    """会话签名/过期、口令定长比对、防开放重定向。"""
    token = issue_session("pw", days=1)
    assert check_session("pw", token)
    assert not check_session("other", token)  # 换口令即全部失效
    assert not check_session("pw", "1700000000.deadbeef")  # 签名不对
    assert not check_session("pw", "1.x")  # 已过期
    assert check_password("pw", "pw") and not check_password("pw", "bad")
    assert not check_password("", "anything")  # 没设口令时不算通过
    assert _safe_next("//evil.com/x") == "/"


def test_xueqiu_url_and_flag_names():
    assert xueqiu_url("0.000933") == "https://www.xueqiu.com/S/SZ000933"
    assert xueqiu_url("1.601058") == "https://www.xueqiu.com/S/SH601058"
    assert xueqiu_url("0.300442") == "https://www.xueqiu.com/S/SZ300442"
    assert xueqiu_url("2.920992") == "https://www.xueqiu.com/S/BJ920992"
    assert flag_names("vol_spike,macd_golden") == "突然放量、MACD金叉"
    assert flag_names("") == ""
    assert flag_names("weird_rule") == "weird_rule"


def test_normalize_sort_uses_view_default():
    assert normalize_sort(None, None, "vol_spike") == ("amount_yi", "desc")
    assert normalize_sort(None, None, "boll_break_lower") == ("boll_pos", "asc")
    assert normalize_sort("pct_chg", "asc", "vol_spike") == ("pct_chg", "asc")
    # 不在白名单里的字段/方向 -> 回落到默认
    assert normalize_sort("drop table", "sideways", "macd_golden") == ("amount_yi", "desc")
    assert DEFAULT_VIEW in VIEWS


def test_build_context_filters_by_view_and_date(tmp_path):
    _, store, date = _seed(tmp_path)
    vol = build_context(store, view="vol_spike", date=date)
    assert [row["code"] for row in vol.rows] == ["600001", "600000", "000933", "300001"]
    assert vol.total == 4 and vol.sort == "amount_yi"
    hit = [row for row in vol.rows if row["code"] == "000933"][0]
    assert hit["url"] == "https://www.xueqiu.com/S/SZ000933"
    assert hit["signals_cn"] == "突然放量、MACD金叉"

    macd = build_context(store, view="macd_golden", date=date)
    # 两只都金叉，按成交额降序：601058(9亿) 在前，000933(2亿) 在后
    assert [row["code"] for row in macd.rows] == ["601058", "000933"]
    kdj = build_context(store, view="kdj_golden", date=date)
    assert [row["code"] for row in kdj.rows] == ["601058"]
    boll = build_context(store, view="boll_break_lower", date=date)
    assert [row["code"] for row in boll.rows] == ["300442"]
    assert boll.order == "asc" and boll.sort == "boll_pos"
    # 没有命中的视图：给一个友好的空状态
    store.conn.execute("UPDATE daily_signals SET s_boll_break_lower = 0")
    store.conn.commit()
    clear_cache()  # 手改库不会更新 computed_at，这里显式清缓存（应用内写入会自动失效）
    empty = build_context(store, view="boll_break_lower", date=date)
    assert empty.rows == [] and empty.total == 0
    store.close()


def test_build_context_sorting_search_and_dates(tmp_path):
    _, store, date = _seed(tmp_path)
    # 日期不在缓存里 -> 用最近的一天
    ctx = build_context(store, view="vol_spike", date="1999-01-01")
    assert ctx.date == date
    # 搜索
    ctx = build_context(store, view="macd_golden", date=date, query="058")
    assert [row["code"] for row in ctx.rows] == ["601058"]
    ctx = build_context(store, view="macd_golden", date=date, query="不存在的名字")
    assert ctx.rows == []
    # 自定义排序（成交额升序）+ 截断
    ctx = build_context(
        store, view="macd_golden", date=date, sort="amount_yi", order="asc", limit=1
    )
    assert ctx.total == 2 and len(ctx.rows) == 1
    assert ctx.rows[0]["code"] == "000933"  # 成交额 2 亿 < 9 亿
    # 空库提示
    store.conn.execute("DELETE FROM daily_signals")
    store.conn.commit()
    clear_cache()
    empty = build_context(store, view="vol_spike")
    assert "还没有每日信号缓存" in empty.error
    store.close()


def test_page_cache_hits_and_invalidates(tmp_path):
    """同一个查询走缓存；缓存里一有新数据（computed_at 变化）就自动失效。"""
    _, store, date = _seed(tmp_path)
    clear_cache()
    first = build_context(store, view="vol_spike", date=date)
    second = build_context(store, view="vol_spike", date=date)
    assert second is first  # 命中缓存，同一个对象
    assert second.total == 4
    # 写入者更新了同一行（computed_at 变了）-> 版本变化 -> 重新查询
    store.upsert_daily_signals(
        [
            {
                "secid": "1.600519",
                "date": date,
                "close": 1600.0,
                "pct_chg": 3.0,
                "amount": 5.0e8,
                "s_vol_spike": 1,
            }
        ]
    )
    third = build_context(store, view="vol_spike", date=date)
    assert third is not first
    # 按成交额降序：600001(1.2e9) > 600519(5e8) > 600000(3e8) > 000933(2e8) > 300001(6e7)
    assert [row["code"] for row in third.rows] == [
        "600001",
        "600519",
        "600000",
        "000933",
        "300001",
    ]
    assert third.total == 5
    store.close()


def test_render_html_has_nav_sortable_headers_and_xueqiu_links(tmp_path):
    _, store, date = _seed(tmp_path)
    ctx = build_context(store, view="vol_spike", date=date)
    page = render_html(ctx, title="A股日频信号")
    store.close()
    # 四个导航按钮
    for spec in VIEWS.values():
        assert spec["nav"] in page
    assert 'class="active"' in page
    # 表头可点击排序：涨跌幅与成交额都在
    assert 'data-sort="pct_chg"' in page and 'data-sort="amount_yi"' in page
    # 排序纯前端：表头不带链接，页面里没有任何 sort=/order= 参数（地址栏只有 view + date）
    assert not re.search(r"[?&](sort|order)=", page)
    assert 'name="sort"' not in page and 'name="order"' not in page
    # 排序状态只体现在提示行；用 localStorage 记住上次排序列
    assert 'id="sortState"' in page and "emscan_sort" in page
    # 只显示最新一天：没有日期选择器，但要有"数据日期 + 是否当天 + 更新时刻"的横幅
    assert 'name="date"' not in page
    assert "数据就是今天的" in page or "数据不是当天的" in page
    assert "15:32" in page and "17:15" in page and "北京时间" in page
    # 代码超链接跳雪球
    assert 'href="https://www.xueqiu.com/S/SZ000933"' in page
    # 涨跌染色 + 移动端卡片
    assert 'class="neg"' in page  # 跌为绿色（A 股习惯：涨红跌绿）
    assert 'class="cards"' not in page  # 手机端也是表格，不再用卡片
    # 名称里的 HTML 会被转义（不是当标签渲染）
    ctx2 = build_context(open_store(make_cfg(tmp_path)), view="boll_break_lower", date=date)
    assert "&lt;b&gt;润泽科技&lt;/b&gt;" in render_html(ctx2, title="t")
    assert "<b>润泽科技</b>" not in render_html(ctx2, title="t")


def test_render_html_empty_state(tmp_path):
    cfg = make_cfg(tmp_path)
    store = open_store(cfg)
    page = render_html(build_context(store, view="vol_spike"))
    store.close()
    assert "还没有每日信号缓存" in page


def test_http_server_serves_pages_and_json(tmp_path):
    _, store, date = _seed(tmp_path)
    store.close()
    cfg = make_cfg(tmp_path)
    httpd = serve(lambda: open_store(cfg), host="127.0.0.1", port=0, blocking=False)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with urllib.request.urlopen(f"{base}/", timeout=10) as resp:
            body = resp.read().decode("utf-8")
            assert resp.status == 200 and "放量" in body
            assert "xueqiu.com/S/" in body
        # 视图：页面只认"最新一天 + 默认排序"（排序在浏览器里做，URL 参数不再生效）
        with urllib.request.urlopen(f"{base}/signals/macd_golden", timeout=10) as resp:
            body = resp.read().decode("utf-8")
            assert resp.status == 200
            sids = re.findall(r'<tr data-sid="([^"]+)"', body)
            assert sids and sids[0] == "1.601058"  # 默认成交额降序：9 亿 > 2 亿
        # JSON 接口
        with urllib.request.urlopen(
            f"{base}/api/signals?view=kdj_golden&limit=1", timeout=10
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["view"] == "kdj_golden" and len(payload["rows"]) == 1
            assert payload["rows"][0]["url"].startswith("https://www.xueqiu.com/S/")
        with urllib.request.urlopen(f"{base}/healthz", timeout=10) as resp:
            assert resp.read() == b"ok"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_server_token_guard(tmp_path):
    """口令登录：没登录跳登录页；POST 口令后拿到 Cookie；Basic 认证给脚本用。"""
    _, store, date = _seed(tmp_path)
    store.close()
    cfg = make_cfg(tmp_path)
    httpd = serve(
        lambda: open_store(cfg), host="127.0.0.1", port=0, password="s3cret", blocking=False
    )
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        # 1) 未登录访问首页 -> 303 跳 /login
        opener = urllib.request.build_opener(NoRedirect())
        try:
            opener.open(f"{base}/", timeout=10)
            raise AssertionError("未登录应该被跳转")
        except urllib.error.HTTPError as exc:
            assert exc.code == 303
            assert "/login" in exc.headers.get("Location", "")
        # 2) 登录页能打开，且不泄露数据
        with urllib.request.urlopen(f"{base}/login", timeout=10) as resp:
            page = resp.read().decode("utf-8")
            assert resp.status == 200 and "口令" in page
            assert "xueqiu.com" not in page
        # 3) API 未登录 -> 401 JSON
        try:
            urllib.request.urlopen(f"{base}/api/signals", timeout=10)
            raise AssertionError("未登录的 API 应该 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        # 4) 口令错误 -> 登录页带错误提示
        bad = urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/login",
                data=urllib.parse.urlencode({"password": "wrong", "next": "/"}).encode(),
                method="POST",
            ),
            timeout=10,
        )
        assert "口令不对" in bad.read().decode("utf-8")
        # 5) 口令正确 -> 303 + Set-Cookie，然后带 Cookie 访问首页
        jar = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        with browser.open(
            urllib.request.Request(
                f"{base}/login",
                data=urllib.parse.urlencode(
                    {"password": "s3cret", "next": "/signals/kdj_golden", "remember": "1"}
                ).encode(),
                method="POST",
            ),
            timeout=10,
        ) as resp:
            assert resp.status == 200
            assert "/signals/kdj_golden" in resp.geturl()  # 登录成功后跳到目标页
            assert "KDJ金叉" in resp.read().decode("utf-8")
        assert any(cookie.name == "emscan_session" for cookie in jar)
        with browser.open(f"{base}/signals/kdj_golden", timeout=10) as resp:
            page = resp.read().decode("utf-8")
            assert resp.status == 200 and "KDJ金叉" in page
            assert "退出登录" in page
        # 6) 脚本用 Basic 认证
        token = base64.b64encode(b":s3cret").decode()
        req = urllib.request.Request(
            f"{base}/api/signals?view=vol_spike", headers={"Authorization": f"Basic {token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            assert payload["total"] >= 1
        # 7) 退出登录后 Cookie 失效
        browser.open(f"{base}/logout", timeout=10).read()
        with browser.open(f"{base}/", timeout=10) as resp:
            assert "请输入访问口令" in resp.read().decode("utf-8")  # 又回到登录页
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_healthz_needs_no_login(tmp_path):
    """探活端点不校验口令（只回 ok，不含数据），给 nginx/k8s 用。"""
    _, store, date = _seed(tmp_path)
    store.close()
    cfg = make_cfg(tmp_path)
    httpd = serve(lambda: open_store(cfg), host="127.0.0.1", port=0, password="pw", blocking=False)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{httpd.server_port}/healthz", timeout=10
        ) as r:
            assert r.read() == b"ok"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_page_shows_latest_day_and_staleness_banner(tmp_path):
    """页面只服务"最新一天"：不给日期选择器；数据不是当天时给醒目提示 + 下次更新时间。"""
    _, store, date = _seed(tmp_path)
    # 数据 = 今天：绿色横幅，说明几点更新
    fresh = render_html(build_context(store, view="vol_spike", today=date))
    assert "数据就是今天的" in fresh
    assert "15:32" in fresh and "17:15" in fresh and "北京时间" in fresh
    assert 'name="date"' not in fresh
    # 数据 = 昨天：黄色告警，说明下一个更新点
    stale = render_html(build_context(store, view="vol_spike", today="2026-10-14"))
    assert "数据不是当天的" in stale and date in stale
    assert "2026-10-14 还没有数据" in stale
    assert "下一个更新点" in stale or "大概率不是交易日" in stale
    assert "系统每个交易日" in stale
    # 空库也要有交代
    store.conn.execute("DELETE FROM daily_signals")
    store.conn.commit()
    clear_cache()
    empty = render_html(build_context(store, view="vol_spike", today=date))
    assert "还没有每日信号缓存" in empty
    store.close()


def test_box_view_is_a_first_class_button(tmp_path):
    """第五个按钮"箱体做T"：和放量/金叉同级，展示箱体列，按箱体位置升序。"""
    _, store, date = _seed(tmp_path)
    store.upsert_daily_signals(
        [
            {
                "secid": "1.600000",
                "date": date,
                "close": 10.0,
                "pct_chg": 0.3,
                "volume": 100000.0,
                "amount": 1.0e8,
                "s_box_range": 1,
                "box_pos": 0.25,
                "box_width": 0.09,
                "box_high": 10.9,
                "box_low": 10.0,
                "box_avg_turnover": 3.3,
                "box_avg_amplitude": 3.1,
                "box_days": 40,
            }
        ]
    )
    store.close()
    cfg = make_cfg(tmp_path)
    httpd = serve(lambda: open_store(cfg), host="127.0.0.1", port=0, blocking=False)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        page = urllib.request.urlopen(f"{base}/signals/box_range", timeout=10).read().decode()
        assert "箱体做T" in page  # 导航按钮
        assert "箱体位置" in page and "日均换手%" in page
        assert "600000" in page  # 命中的票出现在表里
        payload = json.loads(
            urllib.request.urlopen(f"{base}/api/signals?view=box_range", timeout=10).read()
        )
        assert payload["sort"] == "box_pos" and payload["order"] == "asc"
        assert [r["code"] for r in payload["rows"]] == ["600000"]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_table_has_pagination_controls(tmp_path):
    """分页：分页栏只在表格上方一条，每页 10/20/50/100 可选；表头吸顶。"""
    _, store, date = _seed(tmp_path)
    ctx = build_context(store, view="vol_spike", date=date)
    page = render_html(ctx, page_size=50)
    assert page.count('class="pager"') == 1
    # 每页行数是下拉框（10/20/50/100），不再是按钮组
    assert '<select id="pageSize">' in page
    for size in ("10", "20", "50", "100"):
        assert f'<option value="{size}"' in page
    # 板块筛选是多选下拉（details+checkbox），默认全选
    assert '<details class="dd">' in page and 'id="boardSummary"' in page
    assert 'type="checkbox"' in page and ", checked>" in page.replace(" checked>", ", checked>")
    assert "emscan_boards" in page  # 选项记在 localStorage
    # 表头吸顶：CSS 用 --sticky-top，JS 量顶部工具条高度写进去
    assert "top:var(--sticky-top,0px)" in page
    assert "syncStickyTop" in page and "setProperty('--sticky-top'" in page
    # 手机端不再有卡片，全端统一表格（行数 == 数据条数）
    assert page.count('data-sid="') == len(ctx.rows)
    assert 'class="cards"' not in page
    # JS 里的分页参数被替换成了真实值
    assert "[10,20,50,100]" in page and "var DEFAULT_SIZE = 50;" in page
    assert "SIZE_KEY = 'emscan_page_size'" in page and "applyPage" in page
    store.close()


def test_every_sortable_column_sorts_server_side(tmp_path):
    """回归：每一列表头点下去都必须 200 且顺序正确。

    （曾经的 bug：code/name/board 来自 join 出来的 symbols 表，SQL 却写成 ``d.code``，
    于是点"代码/名称/板块"直接 500；板块还应该按中文名排序。）
    """
    _, store, date = _seed(tmp_path)
    store.close()
    cfg = make_cfg(tmp_path)
    httpd = serve(lambda: open_store(cfg), host="127.0.0.1", port=0, blocking=False)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        page = urllib.request.urlopen(f"{base}/signals/vol_spike", timeout=10).read().decode()
        columns = re.findall(r'data-sort="([^"]+)"', page.split("<thead>")[1].split("</thead>")[0])
        assert columns, "表头应该有可排序列"

        def cells(path, index, api: bool = False):
            raw = urllib.request.urlopen(f"{base}{path}", timeout=10).read()
            if api:
                payload = json.loads(raw.decode("utf-8"))
                key = columns[index]
                return [row.get(key) for row in payload["rows"]]
            body = raw.decode("utf-8").split("<tbody>")[1]
            out = []
            for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
                tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                if len(tds) > index:
                    out.append(re.sub(r"<[^>]+>", "", tds[index]).strip())
            return out

        for key in columns:
            for direction in ("asc", "desc"):
                values = cells(
                    f"/api/signals?view=vol_spike&sort={key}&order={direction}",
                    columns.index(key),
                    api=True,
                )
                if key == "board":  # API 返回内部键，但排序按中文名
                    values = [BOARD_LABELS.get(str(v), str(v)) for v in values]
                assert values, f"{key}/{direction} 应该返回数据"
                # SQL 是 (col IS NULL), col dir —— 空值必须排在最后
                filled = [v for v in values if v is not None]
                assert all(
                    v is None for v in values[len(filled) :]
                ), f"{key}/{direction} 空值没排在最后：{values}"
                numbers = []
                for value in filled:
                    try:
                        numbers.append(float(str(value).replace("+", "").replace(",", "")))
                    except (TypeError, ValueError):
                        numbers = None
                        break
                if numbers is not None:
                    assert numbers == sorted(
                        numbers, reverse=(direction == "desc")
                    ), f"{key}/{direction} 顺序不对：{numbers[:5]}"
                else:
                    assert filled == sorted(
                        filled, reverse=(direction == "desc")
                    ), f"{key}/{direction} 文本顺序不对：{filled[:5]}"
        # 页面里板块显示的是中文名，且默认排序下也应有序（内部键 → 中文后比较）
        page_board = [
            BOARD_LABELS.get(str(v), str(v))
            for v in cells(
                "/api/signals?view=vol_spike&sort=board&order=asc", columns.index("board"), api=True
            )
        ]
        assert page_board == sorted(page_board)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_login_rate_limit(tmp_path):
    """连续失败到达上限后暂时拒绝（429），防暴力猜口令。"""
    _, store, date = _seed(tmp_path)
    store.close()
    cfg = make_cfg(tmp_path)
    httpd = serve(
        lambda: open_store(cfg), host="127.0.0.1", port=0, password="right", blocking=False
    )
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        last = 0
        for _ in range(9):
            body = urllib.parse.urlencode({"password": "nope", "next": "/"}).encode()
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(f"{base}/login", data=body, method="POST"), timeout=10
                ) as resp:
                    last = resp.status
            except urllib.error.HTTPError as exc:
                last = exc.code
        assert last == 429
    finally:
        httpd.shutdown()
        httpd.server_close()
