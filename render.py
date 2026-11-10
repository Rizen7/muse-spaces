#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把伙计写的 markdown 渲染成单文件静态 HTML（D 夜间刊 / 夜市任务板样式）。
样式在 styles/，模板在 templates/。
"""

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

# 字段分隔正则（原文标题/英文原标题单独处理）
FIELD_RE = re.compile(
    r"(价格[：:]|截止时间[：:]|截止时间未写明|门槛[：:]|费用[：:]|反馈[：:]|来源[：:]|发布时间[：:]|原文链接[：:]|链接[：:])"
)
ITEM_TITLE_RE = re.compile(r"^-\s+\*\*(.+?)\*\*\s*$")
STATUS_RE = re.compile(r"^状态：(已核实|部分失败|失败)")
URL_IN_TEXT_RE = re.compile(r"(?:原文链接|链接)[：:]\s*(https?://\S+)")
RISK_RE = re.compile(r"⚠\s*风险高(?:（[^）]*）)?")

# 重点标签解析
BADGE_RE = re.compile(r"【(重要|热门|实用)】")
BADGE_PRIORITY = {"重要": 0, "热门": 1, "实用": 2}

# 原文标题 / 英文原标题（全/半角冒号），不误匹配「原文链接」
ORIG_TITLE_RE = re.compile(
    r"(?:英文原标题|原文标题)[：:]\s*(.+?)(?=\s*(?:来源[：:]|发布时间[：:]|价格[：:]|截止时间[：:]|截止时间未写明|门槛[：:]|费用[：:]|反馈[：:]|原文链接[：:]|链接[：:]|。|$))"
)

# 价格提取：「到手价 X」或「现价X」或首段价格数字
PRICE_ARRIVE_RE = re.compile(r"到手价\s*([^\s/,，；;。]+)")
PRICE_NOW_RE = re.compile(r"现价\s*([^\s/,，；;。]+)")
PRICE_ORIG_RE = re.compile(r"原价\s*([^\s/,，；;。]+)")


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
        cleaned = []
        for line in raw.splitlines():
            if line.strip().startswith("@import"):
                continue
            cleaned.append(line)
        parts.append("\n".join(cleaned).strip())
    return "\n\n".join(parts) + "\n"


def nav_html(page: str) -> str:
    """站内导航链接（首页/资讯/薅羊毛）"""
    if page == "news":
        links = [("../", "首页", False), ("./", "国内外资讯", True), ("../deals/", "薅羊毛", False)]
    elif page == "deals":
        links = [("../", "首页", False), ("../news/", "国内外资讯", False), ("./", "薅羊毛", True)]
    else:
        links = [("./", "首页", True), ("news/", "国内外资讯", False), ("deals/", "薅羊毛", False)]
    items = []
    for href, label, current in links:
        cls = ' class="current"' if current else ""
        items.append(f'<a href="{esc(href)}"{cls}>{esc(label)}</a>')
    return "".join(items)


def parse_status(first_line: str) -> str | None:
    m = STATUS_RE.match(first_line.strip())
    return m.group(1) if m else None


# ── 标签相关 ──────────────────────────────────────────────────────────────────

def extract_badges(raw_title: str) -> tuple[list[str], str]:
    """从原始标题中提取标签，返回 (标签列表去重, 去掉标签后的纯标题)。"""
    found = BADGE_RE.findall(raw_title)
    seen: dict[str, None] = {}
    for b in found:
        seen[b] = None
    badges = list(seen.keys())
    clean_title = BADGE_RE.sub("", raw_title).strip()
    return badges, clean_title


def badges_html(badges: list[str]) -> str:
    """生成徽章 HTML，顺序固定：重要 > 热门 > 实用。"""
    if not badges:
        return ""
    ordered = sorted(badges, key=lambda b: BADGE_PRIORITY.get(b, 99))
    badge_cls = {"重要": "badge-important", "热门": "badge-hot", "实用": "badge-useful"}
    spans = [
        f'<span class="badge {badge_cls.get(b, "badge-other")}">{esc(b)}</span>'
        for b in ordered
    ]
    return '<span class="badges">' + "".join(spans) + "</span>"


def item_priority(badges: list[str]) -> int:
    if not badges:
        return 999
    return min(BADGE_PRIORITY.get(b, 99) for b in badges)


def data_tags_attr(badges: list[str]) -> str:
    cls_map = {"重要": "important", "热门": "hot", "实用": "useful"}
    vals = [cls_map.get(b, b) for b in badges]
    return ' data-tags="' + esc(" ".join(vals)) + '"'


# ── 条目解析 ──────────────────────────────────────────────────────────────────

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
                flush()
    flush()
    return items


def sort_items_by_badge(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """稳定排序：同栏目内带标签排前（重要>热门>实用），无标签保持原序在后。"""
    def key_fn(item_with_index: tuple[int, tuple[str, str]]) -> tuple[int, int]:
        idx, (raw_title, _body) = item_with_index
        badges, _ = extract_badges(raw_title)
        return (item_priority(badges), idx)

    indexed = list(enumerate(items))
    indexed.sort(key=key_fn)
    return [item for _, item in indexed]


def parse_markdown(path: Path, kind: str) -> dict:
    result = {
        "missing": False,
        "failed": False,
        "status": None,
        "updated": now_updated(),
        "sections": {},
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
                raw_items = split_items(buckets.get((sec, sub), []))
                submap[sub] = sort_items_by_badge(raw_items)
            sections[sec] = submap
        result["sections"] = sections
    else:
        sections = {}
        for sec in expected:
            merged: list[tuple[str, str]] = []
            for (s, sub), block in buckets.items():
                if s == sec:
                    merged.extend(split_items(block))
            sections[sec] = sort_items_by_badge(merged)
        result["sections"] = sections
    return result


# ── 字段提取 ──────────────────────────────────────────────────────────────────

def extract_orig_title(body: str) -> tuple[str | None, str]:
    m = ORIG_TITLE_RE.search(body)
    if not m:
        return None, body
    orig_title = m.group(1).strip().rstrip("。 ")
    start, end = m.start(), m.end()
    new_body = (body[:start] + " " + body[end:]).strip()
    return orig_title, new_body


def extract_url_and_fields(body: str) -> tuple[str | None, str | None, str | None, str, dict[str, str]]:
    """返回 (url, risk, orig_title, desc, fields)。"""
    orig_title, body = extract_orig_title(body)

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
    return url, risk, orig_title, desc, fields


def parse_risk(risk: str | None) -> tuple[str, str]:
    if not risk:
        return "", ""
    m = re.match(r"(⚠\s*风险高)(?:（([^）]*)）)?", risk)
    if not m:
        return risk, ""
    return m.group(1), (m.group(2) or "")


def parse_price(price_str: str) -> tuple[str, str, str, str]:
    """从价格字段文本解析 (main, was, orig_plain, note)。
    规则：
    1. 取出全角（…）/半角(...)括号说明，用「；」连接成 note，从字符串中删除。
    2. 按「 / 」（斜杠两侧都有空格）切成左右两段。
    3. 有右段：main = 右段去掉开头的「到手价」「现价」「到手」及空白；
       左段去掉开头「原价」及空白后，若含数字 → was，否则 → orig_plain。
    4. 无「 / 」：main = 整段（已去括号），无 was。
    5. 去掉 main/was 末尾的「。；; 」。不得编造。
    """
    if not price_str:
        return "", "", "", ""

    # 1. 提取所有全角/半角括号说明，连接成 note
    notes: list[str] = []
    for m in re.finditer(r"（([^）]*)）|\(([^)]*)\)", price_str):
        content = (m.group(1) if m.group(1) is not None else m.group(2)).strip()
        if content:
            notes.append(content)
    note = "；".join(notes)
    cleaned = re.sub(r"（[^）]*）|\([^)]*\)", "", price_str).strip()

    # 2. 按「 / 」切分
    if re.search(r"\s+/\s+", cleaned):
        left, right = re.split(r"\s+/\s+", cleaned, maxsplit=1)
        left = left.strip()
        right = right.strip()

        # 3. 有右段
        main = re.sub(r"^(?:到手价|现价|到手)\s*", "", right).strip()
        left_clean = re.sub(r"^原价\s*", "", left).strip()
        if re.search(r"\d", left_clean):
            was = left_clean
            orig_plain = ""
        else:
            was = ""
            orig_plain = left_clean
    else:
        # 4. 无「 / 」
        main = cleaned
        was = ""
        orig_plain = ""

    # 5. 去掉 main/was 末尾的「。；; 」
    main = main.rstrip("。；; ")
    was = was.rstrip("。；; ")
    if orig_plain:
        orig_plain = orig_plain.rstrip("。；; ")

    return main, was, orig_plain, note


def fmt_time_short(time_str: str) -> str:
    """把「2026-11-10 07:21（北京时间）」简化为「11-10 07:21」。"""
    if not time_str:
        return ""
    # 匹配 YYYY-MM-DD HH:MM
    m = re.search(r"\d{4}-(\d{2}-\d{2})\s+(\d{2}:\d{2})", time_str)
    if m:
        return m.group(1) + " " + m.group(2)
    # 匹配 YYYY-MM-DD（仅日期）
    m2 = re.search(r"\d{4}-(\d{2}-\d{2})", time_str)
    if m2:
        return m2.group(1)
    return time_str


# ── 状态胶囊 ──────────────────────────────────────────────────────────────────

def pill_html(status: str | None, updated: str, failed: bool) -> str:
    """生成顶部状态胶囊 HTML。"""
    if failed and status not in ("已核实", "部分失败"):
        dot_cls = "fail"
        status_txt = "刷新失败"
    elif status == "部分失败":
        dot_cls = "warn"
        status_txt = "部分失败"
    else:
        dot_cls = ""
        status_txt = "已核实"

    # 简化更新时间显示
    time_txt = ""
    m = re.search(r"\d{4}-(\d{2}-\d{2})\s+(\d{2}:\d{2})", updated)
    if m:
        time_txt = f" · {m.group(1)} {m.group(2)} 北京时间"
    elif updated:
        # fallback：截取「更新于」后的内容
        time_txt = " · " + updated.replace("更新于 ", "").replace("（北京时间）", "").strip()

    dot_class = f'pill-dot{" " + dot_cls if dot_cls else ""}'
    return (
        f'<div class="pill">'
        f'<span class="{dot_class}"></span>'
        f'{esc(status_txt)}{esc(time_txt)}'
        f'</div>'
    )


# ── 资讯页渲染 ────────────────────────────────────────────────────────────────

_SEC_CHANNEL = {
    "AI": "AI Channel",
    "科技": "Tech Channel",
    "经济": "Economy Channel",
    "社会": "Society Channel",
    "文化": "Culture Channel",
    "游戏": "Game Channel",
}


def _item_tag_news_html(sec: str, sub: str) -> str:
    cls = "intl" if sub == "国际" else ""
    cls_attr = f' class="item-tag{" " + cls if cls else ""}"'
    return f'<div{cls_attr}>{esc(sec)} · {esc(sub)}</div>'


def render_item_news(raw_title: str, body: str, sec: str, sub: str, is_feat: bool = False) -> str:
    """渲染资讯条目 HTML 片段。"""
    badges, clean_title = extract_badges(raw_title)
    url, risk, orig_title, desc, fields = extract_url_and_fields(body)
    dtags = data_tags_attr(badges)

    card_cls = "feat-item" if is_feat else "item"
    parts = [f'<article class="{card_cls}"{dtags}>']

    # 分区标签 + 重点徽章
    parts.append(_item_tag_news_html(sec, sub))
    bhtml = badges_html(badges)
    if bhtml:
        parts.append(bhtml)

    # 标题 h4
    if url:
        parts.append(
            f'<h4><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(clean_title)}</a></h4>'
        )
    else:
        parts.append(f"<h4>{esc(clean_title)}</h4>")

    # 原文标题副标题
    if orig_title:
        parts.append(f'<p class="title-en">{esc(orig_title)}</p>')

    # 风险
    label, reason = parse_risk(risk)
    if label:
        parts.append(f'<span class="risk">{esc(label)}</span>')
        if reason:
            parts.append(f'<span class="risk-reason">{esc(reason)}</span>')

    # 摘要
    if desc:
        parts.append(f'<p class="desc">{esc(desc)}</p>')

    # 底部：来源·时间 / 打开原文
    source = fields.get("来源", "")
    pub_time = fields.get("发布时间", "")
    time_short = fmt_time_short(pub_time) if pub_time else ""
    source_time = esc(source)
    if time_short:
        source_time = esc(source + " · " + time_short if source else time_short)

    parts.append('<div class="item-foot">')
    if source_time:
        parts.append(f'<span class="source-time">{source_time}</span>')
    if url:
        parts.append(
            f'<a class="open-link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">打开原文 →</a>'
        )
    parts.append("</div>")

    parts.append("</article>")
    return "\n".join(parts)


def render_news_content(data: dict) -> tuple[str, str, str]:
    """返回 (content_html, sec_nav_html, pill_html_str)。"""
    pill = pill_html(data.get("status"), data["updated"], data.get("failed", False))

    if data["failed"]:
        return '<p class="fail-panel">本次刷新失败</p>', "", pill

    # 栏目导航
    sec_links = []
    for sec in NEWS_SECTIONS:
        sid = f"sec-{sec}"
        sec_links.append(f'<a href="#{sid}">{esc(sec)}</a>')
    sec_nav = '<nav class="sec-nav">' + "".join(sec_links) + "</nav>"

    parts = ['<div class="sections">']
    feat_done = False  # 封面任务只做一次

    for sec, submap in data["sections"].items():
        sid = f"sec-{sec}"
        channel_label = _SEC_CHANNEL.get(sec, sec + " Channel")
        parts.append(f'<section id="{sid}">')
        parts.append(f'<div class="section-header">{esc(channel_label)}</div>')

        for sub in ("国内", "国际"):
            items = submap.get(sub) or []
            if not items:
                continue
            parts.append('<div class="subsection">')
            if items:
                # 封面任务：第一个带标签条目（全局最多一次）
                start_idx = 0
                if not feat_done and items:
                    first_badges, _ = extract_badges(items[0][0])
                    if first_badges:
                        raw_title, body = items[0]
                        parts.append(render_item_news(raw_title, body, sec, sub, is_feat=True))
                        start_idx = 1
                        feat_done = True

                remaining = items[start_idx:]
                if remaining:
                    parts.append('<div class="item-grid">')
                    for raw_title, body in remaining:
                        parts.append(render_item_news(raw_title, body, sec, sub))
                    parts.append("</div>")
                elif start_idx == 0:
                    parts.append('<p class="empty">暂无条目</p>')
            parts.append("</div>")  # subsection

        parts.append("</section>")

    parts.append("</div>")
    return "\n".join(parts), sec_nav, pill


# ── 薅羊毛页渲染 ──────────────────────────────────────────────────────────────

_CAT_CLS = {
    "白嫖": "free",
    "限免": "limited",
    "低价": "deal",
    "币圈空投": "airdrop",
    "NFT": "nft",
}


def render_item_deals(raw_title: str, body: str, sec: str, is_feat: bool = False) -> str:
    """渲染薅羊毛条目 HTML 片段。"""
    badges, clean_title = extract_badges(raw_title)
    url, risk, orig_title, desc, fields = extract_url_and_fields(body)
    dtags = data_tags_attr(badges)
    cat_data = f' data-cat="{esc(sec)}"'

    card_cls = "feat-item" if is_feat else "item"
    parts = [f'<article class="{card_cls}"{dtags}{cat_data}>']

    # 栏目标签
    cat_cls = _CAT_CLS.get(sec, "deal")
    parts.append(f'<div class="cat-tag {cat_cls}">{esc(sec)}</div>')

    # 重点徽章
    bhtml = badges_html(badges)
    if bhtml:
        parts.append(bhtml)

    # 标题 h3
    if url:
        parts.append(
            f'<h3><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(clean_title)}</a></h3>'
        )
    else:
        parts.append(f"<h3>{esc(clean_title)}</h3>")

    # 原文标题副标题
    if orig_title:
        parts.append(f'<p class="title-en">{esc(orig_title)}</p>')

    # 价格行（从「价格」字段提取）
    price_raw = fields.get("价格", "")
    if price_raw:
        main, was, orig_plain, note = parse_price(price_raw)
        long_cls = " price-long" if len(main) > 12 else ""
        was_html = f'<span class="price-was">{esc(was)}</span>' if was else ""
        parts.append(
            f'<div class="price-row"><span class="price-main{long_cls}">{esc(main)}</span>{was_html}</div>'
        )
        if orig_plain:
            parts.append(f'<p class="price-orig">原价：{esc(orig_plain)}</p>')
        if note:
            parts.append(f'<p class="price-note">{esc(note)}</p>')

    # 风险（放在价格下方，摘要上方）
    label, reason = parse_risk(risk)
    if label:
        risk_txt = esc(label) + (f"（{esc(reason)}）" if reason else "")
        parts.append(f'<div class="risk">⚠ {risk_txt}</div>')

    # 摘要
    if desc:
        parts.append(f'<p class="desc">{esc(desc)}</p>')

    # 字段（门槛/截止/费用/反馈，不含价格）
    field_order = ["截止时间", "门槛", "费用", "反馈"]
    field_rows = []
    for key in field_order:
        if fields.get(key):
            field_rows.append(
                f'<div><dt>{esc(key)}</dt>：<dd>{esc(fields[key])}</dd></div>'
            )
    if field_rows:
        parts.append('<dl class="fields">' + "".join(field_rows) + "</dl>")

    # 底部：来源 + 领取/查看
    source = fields.get("来源", "")
    parts.append('<div class="item-foot">')
    if source:
        parts.append(f'<span class="source-name">{esc(source)}</span>')
    if url:
        btn_txt = "领取 / 查看 →"
        parts.append(
            f'<a class="open-link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">{btn_txt}</a>'
        )
    parts.append("</div>")

    parts.append("</article>")
    return "\n".join(parts)


def render_deals_content(data: dict) -> tuple[str, str, str]:
    """返回 (content_html, tabs_html, pill_html_str)。"""
    pill = pill_html(data.get("status"), data["updated"], data.get("failed", False))

    if data["failed"]:
        return '<p class="fail-panel">本次刷新失败</p>', "", pill

    # 筛选 tabs
    tab_btns = ['<button type="button" class="on" data-cat="all">全部任务</button>']
    for sec in DEALS_SECTIONS:
        tab_btns.append(f'<button type="button" data-cat="{esc(sec)}">{esc(sec)}</button>')
    tabs_html = '<div class="tabs" id="tabs">' + "".join(tab_btns) + "</div>"

    parts = ['<div class="boards" id="board">']
    feat_done = False

    for sec, items in data["sections"].items():
        if not items:
            continue
        parts.append(f'<section class="board" data-sec="{esc(sec)}">')
        parts.append(f'<div class="board-header">{esc(sec)}</div>')

        # 封面任务（全局最多一次，第一个带标签条目）
        start_idx = 0
        if not feat_done and items:
            first_badges, _ = extract_badges(items[0][0])
            if first_badges:
                raw_title, body = items[0]
                parts.append(render_item_deals(raw_title, body, sec, is_feat=True))
                start_idx = 1
                feat_done = True

        remaining = items[start_idx:]
        if remaining:
            parts.append('<div class="board-grid">')
            for raw_title, body in remaining:
                parts.append(render_item_deals(raw_title, body, sec))
            parts.append("</div>")
        elif start_idx == 0:
            parts.append('<p class="empty">暂无条目</p>')

        parts.append("</section>")

    parts.append("</div>")
    return "\n".join(parts), tabs_html, pill


# ── 筛选 JS ───────────────────────────────────────────────────────────────────

FILTER_JS = """<script>
(function(){
  var tabs=document.getElementById('tabs');
  var board=document.getElementById('board');
  if(!tabs||!board) return;
  var items=[].slice.call(board.querySelectorAll('[data-cat]'));
  tabs.addEventListener('click',function(e){
    var b=e.target;
    if(b.tagName!=='BUTTON') return;
    [].forEach.call(tabs.querySelectorAll('button'),function(x){x.classList.toggle('on',x===b);});
    var cat=b.dataset.cat;
    items.forEach(function(t){
      t.style.display=(cat==='all'||t.dataset.cat===cat)?'':'none';
    });
  });
})();
</script>"""


# ── 辅助：pill 和 banner ──────────────────────────────────────────────────────

def banner_html(sample: bool) -> str:
    return '<p class="banner-sample">试跑样例</p>\n' if sample else ""


def _status_for_meta(status: str | None) -> str:
    if status in ("已核实", "部分失败", "失败"):
        return status
    return ""


# ── 首页：从已渲染 HTML 读取更新时间和状态 ────────────────────────────────────

def _read_meta_from_html(html_path: Path) -> tuple[str, str]:
    if not html_path.is_file():
        return "", ""
    content = html_path.read_text(encoding="utf-8", errors="replace")
    updated = ""
    status = ""
    m = re.search(r'<meta\s+name="muse-updated"\s+content="([^"]*)"', content)
    if m:
        updated = m.group(1).strip()
    else:
        m2 = re.search(r"更新于\s*\S+.*?（北京时间）", content)
        if m2:
            updated = m2.group(0).strip()
    m3 = re.search(r'<meta\s+name="muse-status"\s+content="([^"]*)"', content)
    if m3:
        status = m3.group(1).strip()
    return updated, status


def render_index_page(out_path: Path | None, sample: bool) -> str:
    if out_path is not None:
        base_dir = out_path.parent
    else:
        base_dir = ROOT

    news_updated_txt, news_status = _read_meta_from_html(base_dir / "news" / "index.html")
    deals_updated_txt, deals_status = _read_meta_from_html(base_dir / "deals" / "index.html")

    def fmt_updated(txt: str) -> str:
        return esc(txt) if txt else "暂无更新"

    def fmt_status(status_val: str) -> str:
        if status_val == "失败":
            return ' <span class="status-fail">本次刷新失败</span>'
        if status_val == "部分失败":
            return ' <span class="status-partial">部分失败</span>'
        return ""

    return fill_template(
        "index.html",
        {
            "title": "公开资讯页",
            "css": load_css("common.css", "index.css"),
            "banner": banner_html(sample),
            "news_updated": fmt_updated(news_updated_txt),
            "deals_updated": fmt_updated(deals_updated_txt),
            "news_status": fmt_status(news_status),
            "deals_status": fmt_status(deals_status),
        },
    )


# ── 主渲染入口 ────────────────────────────────────────────────────────────────

def render_page(kind: str, source: Path | None, sample: bool = False, out_path: Path | None = None) -> str:
    if kind == "index":
        return render_index_page(out_path, sample)

    data = parse_markdown(source, kind) if source else {
        "missing": True,
        "failed": True,
        "status": None,
        "updated": now_updated(),
        "sections": {},
    }

    if kind == "news":
        content, sec_nav, pill = render_news_content(data)
        return fill_template(
            "news.html",
            {
                "title": "国内外资讯",
                "css": load_css("common.css", "news.css"),
                "nav": nav_html("news"),
                "banner": banner_html(sample),
                "updated": esc(data["updated"]),
                "muse_status": esc("失败" if data.get("failed") else _status_for_meta(data.get("status"))),
                "pill": pill,
                "sec_nav": sec_nav,
                "content": content,
            },
        )

    # deals
    content, tabs, pill = render_deals_content(data)
    return fill_template(
        "deals.html",
        {
            "title": "薅羊毛",
            "css": load_css("common.css", "deals.css"),
            "nav": nav_html("deals"),
            "banner": banner_html(sample),
            "updated": esc(data["updated"]),
            "muse_status": esc("失败" if data.get("failed") else _status_for_meta(data.get("status"))),
            "pill": pill,
            "tabs": tabs,
            "content": content,
            "filter_js": FILTER_JS,
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

    out = Path(args.out).resolve() if args.out else default_out(args.type)
    out.parent.mkdir(parents=True, exist_ok=True)

    html_out = render_page(args.type, source, sample=args.sample, out_path=out)
    out.write_text(html_out, encoding="utf-8")
    print(str(out), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
