"""网页端测试：导航/四个视图、排序、搜索、雪球链接、HTML 转义、HTTP 服务。"""

import json
import urllib.error
import urllib.request

from helpers import make_cfg

from emscan.cli import open_store
from emscan.web import (
    DEFAULT_VIEW,
    VIEWS,
    build_context,
    clear_cache,
    flag_names,
    normalize_sort,
    render_html,
    serve,
    xueqiu_url,
)


def _seed(tmp_path, config=None):
    cfg = make_cfg(tmp_path, config or {})
    store = open_store(cfg)
    store.upsert_symbols(
        [
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
    assert [row["code"] for row in vol.rows] == ["000933"]  # 只有它 s_vol_spike=1
    assert vol.total == 1 and vol.sort == "amount_yi"
    assert vol.rows[0]["url"] == "https://www.xueqiu.com/S/SZ000933"
    assert vol.rows[0]["signals_cn"] == "突然放量、MACD金叉"

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
    assert second.total == 1
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
    assert [row["code"] for row in third.rows] == ["600519", "000933"]  # 5 亿 > 2 亿
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
    assert "sort=pct_chg" in page
    # 代码超链接跳雪球
    assert 'href="https://www.xueqiu.com/S/SZ000933"' in page
    # 涨跌染色 + 移动端卡片
    assert 'class="neg"' in page and 'class="cards"' in page
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
        # 视图 + 服务器端排序
        with urllib.request.urlopen(
            f"{base}/signals/macd_golden?date={date}&sort=pct_chg&order=asc", timeout=10
        ) as resp:
            body = resp.read().decode("utf-8")
            first = body.split("<tbody>")[1].split("<tr>")[1]
            assert "000933" in first  # -3.5% 排第一
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
    _, store, date = _seed(tmp_path)
    store.close()
    cfg = make_cfg(tmp_path)
    httpd = serve(lambda: open_store(cfg), host="127.0.0.1", port=0, token="s3cret", blocking=False)
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        try:
            urllib.request.urlopen(f"{base}/", timeout=10)
            raise AssertionError("没有 token 应该 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        with urllib.request.urlopen(f"{base}/?token=s3cret", timeout=10) as resp:
            assert resp.status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
