"""零依赖的 Markdown 子集渲染（只用于复盘正文）。

复盘是 AI 生成的内容，必须先 ``html.escape`` 再做 Markdown 转换，避免把
``<script>`` 之类的标签原样塞进页面。这里覆盖复盘实际会用到的语法：

* 标题 ``#`` ~ ``######``、水平线
* 段落、**粗体**、*斜体*、`行内代码`、链接、删除线
* 有序/无序列表（单层即可）、引用块
* 代码块（``` 围栏）与 GFM 表格

没有引第三方库：服务器不用额外 pip install，也不会因为 CDN 不可达变成白屏。
"""

from __future__ import annotations

import html
import re
from typing import List, Sequence

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_FENCE_RE = re.compile(r"^\s*```([\w+-]*)\s*$")
_FENCE_END_RE = re.compile(r"^\s*```\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_ALT_RE = re.compile(r"__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", re.I)

_CJK_PUNCT = "，。；：、！？）》」』”’%,.;:!?)]}"


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def _join_lines(lines: Sequence[str]) -> str:
    """把段落里的软换行拼起来：中文之间不加空格，英文/数字之间加一个空格。"""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        left, right = out[-1], line[0]
        if (_is_cjk(left) or left in _CJK_PUNCT) and (_is_cjk(right) or right in _CJK_PUNCT):
            out += line
        else:
            out += " " + line
    return out


def _inline(text: str) -> str:
    """处理行内语法；传入的 text 必须已经做过 HTML 转义。"""
    codes: List[str] = []

    def _stash(match: re.Match) -> str:
        codes.append(match.group(1))
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_SPAN_RE.sub(_stash, text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)
    text = _BOLD_ALT_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    text = _STRIKE_RE.sub(r"<del>\1</del>", text)
    text = _LINK_RE.sub(
        lambda m: (
            f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>'
        ),
        text,
    )
    for index, code in enumerate(codes):
        text = text.replace(f"\x00{index}\x00", f"<code>{code}</code>")
    return text


def _starts_block(lines: Sequence[str], index: int) -> bool:
    line = lines[index]
    if not line.strip():
        return True
    if (
        _FENCE_RE.match(line)
        or _HEADING_RE.match(line)
        or _HR_RE.match(line)
        or line.lstrip().startswith(">")
        or _UL_RE.match(line)
        or _OL_RE.match(line)
    ):
        return True
    return bool("|" in line and index + 1 < len(lines) and _TABLE_SEP_RE.match(lines[index + 1]))


def _split_row(line: str) -> List[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def render_markdown(text: str) -> str:
    """把复盘 Markdown 渲染成安全的 HTML 片段。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    i, total = 0, len(lines)

    while i < total:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            lang = fence.group(1)
            code_lines: List[str] = []
            i += 1
            while i < total and not _FENCE_END_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code_lines))}</code></pre>")
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            title = _inline(html.escape(heading.group(2).strip()))
            out.append(f"<h{level}>{title}</h{level}>")
            i += 1
            continue

        if line.lstrip().startswith(">"):
            quoted: List[str] = []
            while i < total and lines[i].lstrip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + render_markdown("\n".join(quoted)) + "</blockquote>")
            continue

        ordered = bool(_OL_RE.match(line))
        if ordered or _UL_RE.match(line):
            items: List[str] = []
            matcher = _OL_RE if ordered else _UL_RE
            while i < total and lines[i].strip():
                match = matcher.match(lines[i])
                if not match:
                    break
                items.append(match.group(2) if ordered else match.group(1))
                i += 1
            tag = "ol" if ordered else "ul"
            body = "".join(f"<li>{_inline(html.escape(item.strip()))}</li>" for item in items)
            out.append(f"<{tag}>{body}</{tag}>")
            continue

        if "|" in line and i + 1 < total and _TABLE_SEP_RE.match(lines[i + 1]):
            header = _split_row(line)
            i += 2
            rows: List[List[str]] = []
            while i < total and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            head = "".join(f"<th>{_inline(html.escape(cell))}</th>" for cell in header)
            body = "".join(
                "<tr>" + "".join(f"<td>{_inline(html.escape(cell))}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            continue

        paragraph: List[str] = []
        while i < total and not _starts_block(lines, i):
            paragraph.append(lines[i])
            i += 1
        if not paragraph:  # 兜底：绝不空转
            paragraph = [line]
            i += 1
        out.append(f"<p>{_inline(html.escape(_join_lines(paragraph)))}</p>")

    return "\n".join(out)
