"""当日总结页的 Markdown 渲染：结构、安全转义与页面排版。"""

from emscan.markdown_render import render_markdown
from emscan.web import render_summary_notice, render_summary_page

DOC = """# 2026-10-19 复盘

## 一、今日全景

今天 **放量** 明显，涨跌幅中位数 `1.2%`。

- 第一项 **加粗**
- 第二项 [链接](https://github.com/ingaml4/daily_freq_stock_data)

| 指标 | 数量 |
| --- | ---: |
| MACD | 0 |

> 提示：布林下轨数量多≠马上反转。

---

TIER1: 600001 测试
"""


def test_render_common_summary_markdown():
    body = render_markdown(DOC)
    assert "<h1>2026-10-19 复盘</h1>" in body
    assert "<h2>一、今日全景</h2>" in body
    assert "<strong>放量</strong>" in body
    assert "<code>1.2%</code>" in body
    assert "<ul>" in body and "<li>第一项 <strong>加粗</strong></li>" in body
    assert 'href="https://github.com/ingaml4/daily_freq_stock_data"' in body
    assert "<table>" in body and "<th>指标</th>" in body
    assert "<blockquote>" in body and "<hr>" in body
    assert "<p>TIER1: 600001 测试</p>" in body
    # 没有把 Markdown 原样塞进 <pre>
    assert "## 一、今日全景" not in body


def test_render_escapes_html_and_blocks_javascript_links():
    body = render_markdown(
        "<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n<img src=x onerror=alert(1)>"
    )
    assert "&lt;script&gt;" in body
    assert "<script>" not in body
    assert "javascript:" in body  # 仅作为纯文本保留，不会变成 href
    assert 'href="javascript:' not in body
    assert "<img" not in body


def test_render_joins_cjk_lines_and_keeps_code_fence():
    assert "<p>第一行第二行</p>" in render_markdown("第一行\n第二行")
    body = render_markdown('```python\nprint("<x>")\n```')
    assert '<pre><code class="language-python">' in body
    assert "&lt;x&gt;" in body
    assert "<x>" not in body


def test_summary_page_is_styled_html_not_raw_pre():
    page = render_summary_page(
        {"date": "2026-10-19", "engine": "deepseek-flash(thinking=high)", "content": DOC}
    )
    assert '<article class="md">' in page
    assert "<h2>一、今日全景</h2>" in page
    assert "<table>" in page
    assert "下载 Markdown" in page
    assert "deepseek-flash(thinking=high)" in page
    assert "&lt;h2&gt;" not in page
    # 样式在页面内，不依赖 CDN
    assert "<style>" in page and "cdn" not in page.lower()


def test_summary_notice_is_styled():
    page = render_summary_notice("当日总结还没生成")
    assert "当日总结还没生成" in page
    assert 'class="notice"' in page
