#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把伙计写的 markdown 渲染成单文件静态 HTML。样式在 styles/，模板在 templates/。"""

from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STYLES = ROOT / "styles"
TEMPLATES = ROOT / "templates"
BJ = timezone(timedelta(hours=8))

NEWS_SECTIONS = ["AI", "科技", "经济", "社会", "文化", "游戏"]
DEALS_SECTIONS = ["白嫖", "限免", "低价", "币圈空投", "NFT"]
FIELD_RE = re.compile(
    r"(价格[：:]|截止时间[：:]|截止时间未写明|门槛[：:]|费用[：:]|反馈[：:]|来源[：:]|原文链接[：:]|链接[：:])"
)
ITEM_TITLE_RE = re.compile(r"^-\s+\*\*(.+?)\*\*\s*$")
STATUS_RE = re.compile(r"^状态：(已核实|部分失败|失败)")
URL_IN_TEXT_RE = re.compile(r"(?:原文链接|链接)[：:]\s*(https?://\S+)")
RISK_RE = re.compile(r"⚠\s*风险高(?:（[^）]*）)?")


def esc(text: str) -> str:
    return html.escape(text or "", quote=True)


def now_updated() -> str:
    return datetime.now(BJ).strftime("更新于 %Y-%m-%d %H:%M（北京时间）")


def extract_updated(lines: list[str]) -> str | None:
    for line in lines[:12]:
        s = line.strip()
        if s.startswith("更新于"):
            return s
    return None


def strip_url_punct(url: str) -> str:
    return url.rstrip("。.;,，、 ")


def fill_template(name: str, mapping: dict[str, str]) -> str:
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    # 先替换非 css 占位符，最后注入 css，避免样式里偶发 {{ 被误伤（当前样式不含）。
    css = mapping.get("css", "")
    for key, value in mapping.items():
        if key == "css":
            continue
        text = text.replace("{{" + key + "}}", value)
    text = text.replace("{{css}}", css)
    leftover = re.findall(r"\{\{[a-z_]+\}\}", text)
    if leftover:
        raise RuntimeError(f"模板 {name} 还有未替换占位符: {leftover}")
    return text


def load_css(*names: str) -> str:
    parts = []
    for name in names:
        raw = (STYLES / name).read_text(encoding="utf-8")
        # 去掉仅作说明的 @import 行，改由本函数按顺序内联
        cleaned = []
        for line in raw.splitlines():
            if line.strip().startswith("@import"):
                continue
            cleaned.append(line)
        parts.append("\n".join(cleaned).strip())
    return "\n\n".join(parts) + "\n"


def nav_html(page: str) -> str:
    if page == "news":
        links = [
            ("../", "首页", False),
            ("./", "国内外资讯", True),
            ("../deals/", "薅羊毛", False),
        ]
    elif page == "deals":
        links = [
            ("../", "首页", False),
            ("../news/", "国内外资讯", False),
            ("./", "薅羊毛", True),
        ]
    else:
        links = [
            ("./", "首页", True),
            ("news/", "国内外资讯", False),
            ("deals/", "薅羊毛", False),
        ]
    items = []
    for href, label, current in links:
        cls = ' class="current"' if current else ""
        items.append(f'<a href="{esc(href)}"{cls}>{esc(label)}</a>')
    return '<nav class="site-nav">' + "".join(items) + "</nav>\n"


def parse_status(first_line: str) -> str | None:
    m = STATUS_RE.match(first_line.strip())
    return m.group(1) if m else None


def split_items(block_lines: list[str]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    title = None
    body: list[str] = []

    def flush() -> None:
        nonlocal title, body
        if title is None:
            body = []
            return
        items.append((title, " ".join(x.strip() for x in body if x.strip())))
        title = None
        body = []

    for line in block_lines:
        m = ITEM_TITLE_RE.match(line)
        if m:
            flush()
            title = m.group(1).strip()
            continue
        if title is not None:
            if line.startswith(" ") or line.startswith("\t"):
                body.append(line)
            elif line.strip() == "":
                continue
            else:
                # 非缩进行结束本条，避免把文末说明写进最后一条
                flush()
    flush()
    return items


def parse_markdown(path: Path, kind: str) -> dict:
    result = {
        "missing": False,
        "failed": False,
        "status": None,
        "updated": now_updated(),
        "sections": {},
        "subsections": {},
    }
    if not path.is_file():
        result["missing"] = True
        result["failed"] = True
        return result

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    first = lines[0].strip() if lines else ""
    status = parse_status(first)
    result["status"] = status
    updated = extract_updated(lines)
    if updated:
        result["updated"] = updated
    if status == "失败":
        result["failed"] = True
        return result

    expected = NEWS_SECTIONS if kind == "news" else DEALS_SECTIONS
    current_sec = None
    current_sub = "条目"
    buckets: dict[tuple[str, str], list[str]] = {}

    for line in lines[1:]:
        if line.startswith("## "):
            current_sec = line[3:].strip()
            current_sub = "条目"
            continue
        if line.startswith("### "):
            current_sub = line[4:].strip()
            continue
        if current_sec is None:
            continue
        buckets.setdefault((current_sec, current_sub), []).append(line)

    if kind == "news":
        sections = {}
        for sec in expected:
            submap = {}
            for sub in ("国内", "国际"):
                submap[sub] = split_items(buckets.get((sec, sub), []))
            sections[sec] = submap
        result["sections"] = sections
    else:
        sections = {}
        for sec in expected:
            # 薅羊毛没有国内/国际，条目都在默认「条目」或该标题下任意子块合并
            merged: list[tuple[str, str]] = []
            for (s, sub), block in buckets.items():
                if s == sec:
                    merged.extend(split_items(block))
            sections[sec] = merged
        result["sections"] = sections
    return result


def extract_url_and_fields(body: str) -> tuple[str | None, str | None, str, dict[str, str]]:
    risk = None
    m = RISK_RE.search(body)
    if m:
        risk = m.group(0).strip()
        body = (body[: m.start()] + " " + body[m.end() :]).strip()

    url = None
    m = URL_IN_TEXT_RE.search(body)
    if m:
        url = strip_url_punct(m.group(1))
        body = (body[: m.start()] + " " + body[m.end() :]).strip()

    parts = FIELD_RE.split(body)
    desc = parts[0].strip()
    fields: dict[str, str] = {}
    i = 1
    while i < len(parts):
        raw_label = parts[i]
        value = parts[i + 1].strip() if i + 1 < len(parts) else ""
        value = value.strip("。;； ")
        if raw_label == "截止时间未写明":
            extra = value.strip("（）()")
            fields["截止时间"] = "未写明" + (f"（{extra}）" if extra else "")
        else:
            label = re.sub(r"[：:]$", "", raw_label)
            if label in ("原文链接", "链接"):
                if value.startswith("http"):
                    url = strip_url_punct(value.split()[0])
            else:
                fields[label] = value
        i += 2
    desc = desc.strip("。;； ").strip()
    return url, risk, desc, fields


def parse_risk(risk: str | None) -> tuple[str, str]:
    if not risk:
        return "", ""
    m = re.match(r"(⚠\s*风险高)(?:（([^）]*)）)?", risk)
    if not m:
        return risk, ""
    return m.group(1), (m.group(2) or "")


def render_news_content(data: dict) -> tuple[str, str]:
    if data["failed"]:
        return '<p class="fail-panel">本次刷新失败</p>', ""
    toc = ['<ul class="toc">']
    parts = ['<div class="sections">']
    for sec, submap in data["sections"].items():
        sid = f"sec-{esc(sec)}"
        toc.append(f'<li><a href="#{sid}">{esc(sec)}</a></li>')
        parts.append(f'<section class="section" id="{sid}">')
        parts.append(f"<h2>{esc(sec)}</h2>")
        for sub in ("国内", "国际"):
            items = submap.get(sub) or []
            parts.append(f'<div class="subsection"><h3>{esc(sub)}</h3>')
            if not items:
                parts.append('<p class="empty">暂无条目</p>')
            for title, body in items:
                url, risk, desc, fields = extract_url_and_fields(body)
                parts.append('<article class="item">')
                if url:
                    parts.append(f'<h4><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(title)}</a></h4>')
                else:
                    parts.append(f"<h4>{esc(title)}</h4>")
                if desc:
                    parts.append(f'<p class="desc">{esc(desc)}</p>')
                meta_bits = []
                if fields.get("来源"):
                    meta_bits.append("来源：" + fields["来源"])
                if fields.get("发布时间"):
                    meta_bits.append("发布时间：" + fields["发布时间"])
                # 若字段拆分失败，尝试从原文里补来源和时间
                if not meta_bits:
                    sm = re.search(r"来源：([^。]+)", body)
                    tm = re.search(r"发布时间：([^。]+)", body)
                    if sm:
                        meta_bits.append("来源：" + sm.group(1).strip())
                    if tm:
                        meta_bits.append("发布时间：" + tm.group(1).strip())
                if meta_bits:
                    parts.append(f'<p class="meta">{esc(" ｜ ".join(meta_bits))}</p>')
                parts.append("</article>")
            parts.append("</div>")
        parts.append("</section>")
    parts.append("</div>")
    toc.append("</ul>")
    return "\n".join(parts), "\n".join(toc)


def render_deals_content(data: dict) -> str:
    if data["failed"]:
        return '<p class="fail-panel">本次刷新失败</p>'
    parts = ['<div class="boards">']
    for sec, items in data["sections"].items():
        sid = f"sec-{esc(sec)}"
        parts.append(f'<section class="board" id="{sid}">')
        parts.append(f"<h2>{esc(sec)}</h2>")
        if not items:
            parts.append('<p class="empty">暂无条目</p>')
        for title, body in items:
            url, risk, desc, fields = extract_url_and_fields(body)
            parts.append('<article class="item">')
            if url:
                parts.append(f'<h3><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(title)}</a></h3>')
            else:
                parts.append(f"<h3>{esc(title)}</h3>")
            label, reason = parse_risk(risk)
            if label:
                parts.append(f'<span class="risk">{esc(label)}</span>')
                if reason:
                    parts.append(f'<span class="risk-reason">{esc(reason)}</span>')
            if desc:
                parts.append(f'<p class="desc">{esc(desc)}</p>')
            shown = []
            order = ["价格", "截止时间", "门槛", "费用", "反馈"]
            field_rows = []
            for key in order:
                if fields.get(key):
                    field_rows.append(
                        f"<div><dt>{esc(key)}</dt>：<dd>{esc(fields[key])}</dd></div>"
                    )
                    shown.append(key)
            if field_rows:
                parts.append('<dl class="fields">' + "".join(field_rows) + "</dl>")
            if fields.get("来源"):
                parts.append(f'<p class="source">来源：{esc(fields["来源"])}</p>')
            parts.append("</article>")
        parts.append("</section>")
    parts.append("</div>")
    return "\n".join(parts)


def banner_html(sample: bool) -> str:
    return '<p class="banner-sample">试跑样例</p>\n' if sample else ""


def status_note_html(status: str | None) -> str:
    if status == "部分失败":
        return '<p class="status-partial">状态：部分失败</p>\n'
    return ""


def render_page(kind: str, source: Path | None, sample: bool = False) -> str:
    if kind == "index":
        return fill_template(
            "index.html",
            {
                "title": "公开资讯页",
                "css": load_css("common.css", "index.css"),
                "banner": banner_html(sample),
            },
        )

    data = parse_markdown(source, kind) if source else {
        "missing": True,
        "failed": True,
        "status": None,
        "updated": now_updated(),
        "sections": {},
    }
    if kind == "news":
        content, toc = render_news_content(data)
        return fill_template(
            "news.html",
            {
                "title": "国内外资讯",
                "css": load_css("common.css", "news.css"),
                "nav": nav_html("news"),
                "banner": banner_html(sample),
                "updated": esc(data["updated"]),
                "status_note": status_note_html(data.get("status")),
                "toc": toc,
                "content": content,
            },
        )
    content = render_deals_content(data)
    return fill_template(
        "deals.html",
        {
            "title": "薅羊毛",
            "css": load_css("common.css", "deals.css"),
            "nav": nav_html("deals"),
            "banner": banner_html(sample),
            "updated": esc(data["updated"]),
            "status_note": status_note_html(data.get("status")),
            "content": content,
        },
    )


def default_out(kind: str) -> Path:
    if kind == "index":
        return ROOT / "index.html"
    return ROOT / kind / "index.html"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="渲染资讯或薅羊毛静态页")
    ap.add_argument("type", choices=["news", "deals", "index"])
    ap.add_argument("source", nargs="?", help="伙计写的 markdown 路径（index 不需要）")
    ap.add_argument("--out", help="输出 HTML 路径")
    ap.add_argument("--sample", action="store_true", help="页面顶部标明「试跑样例」")
    args = ap.parse_args(argv)

    source = Path(args.source).resolve() if args.source else None
    if args.type in ("news", "deals") and not args.source:
        print("news/deals 需要提供源 markdown 路径（文件可以暂不存在）", file=sys.stderr)
        return 2

    html_out = render_page(args.type, source, sample=args.sample)
    out = Path(args.out).resolve() if args.out else default_out(args.type)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_out, encoding="utf-8")
    print(str(out), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
