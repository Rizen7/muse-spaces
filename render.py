#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把伙计写的 markdown 渲染成单文件静态 HTML（D 夜间刊 / 夜市任务板样式）。
样式在 styles/，模板在 templates/。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STYLES = ROOT / "styles"
TEMPLATES = ROOT / "templates"
IMG_DIR = ROOT / "img"
BJ = timezone(timedelta(hours=8))

IMAGE_LINE_RE = re.compile(r"配图[：:]\s*(https?://\S+)")
IMG_NAME_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})-([0-9a-f]{8})\.(jpg|jpeg|png|webp|gif)$"
)
IMG_USER_AGENT = (
    "Mozilla/5.0 (compatible; MuseSpaces/1.0; "
    "+https://rizen7.github.io/muse-spaces/)"
)

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

# 重点标签解析（含【新增】）
BADGE_RE = re.compile(r"【(重要|热门|实用|新增)】")
BADGE_PRIORITY = {"重要": 0, "热门": 1, "实用": 2}
# 【新增】不计入重点优先级，单独处理
NEW_BADGE_RE = re.compile(r"【新增】")

# 上架时间解析（薅羊毛专用）
LISTING_TIME_RE = re.compile(r"上架[：:]\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)")

# 原文标题 / 英文原标题（全/半角冒号），不误匹配「原文链接」
ORIG_TITLE_RE = re.compile(
    r"(?:英文原标题|原文标题)[：:]\s*(.+?)(?=\s*(?:来源[：:]|发布时间[：:]|价格[：:]|截止时间[：:]|截止时间未写明|门槛[：:]|费用[：:]|反馈[：:]|原文链接[：:]|链接[：:]|。|\n|$))"
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
    """从原始标题中提取标签，返回 (标签列表去重含新增, 去掉所有标签后的纯标题)。"""
    found = BADGE_RE.findall(raw_title)
    seen: dict[str, None] = {}
    for b in found:
        seen[b] = None
    badges = list(seen.keys())
    clean_title = BADGE_RE.sub("", raw_title).strip()
    return badges, clean_title


def extract_hot_badges(badges: list[str]) -> list[str]:
    """返回重点标签（重要/热门/实用），排除【新增】。"""
    return [b for b in badges if b in BADGE_PRIORITY]


def badges_html(badges: list[str], include_new: bool = False) -> str:
    """生成徽章 HTML。
    badges 可含「新增」。
    include_new=True 时也渲染 badge-new；否则只渲染重要/热门/实用。
    顺序：新增（若include_new）> 重要 > 热门 > 实用。
    """
    if not badges:
        return ""
    hot = [b for b in badges if b in BADGE_PRIORITY]
    is_new = "新增" in badges
    ordered = sorted(hot, key=lambda b: BADGE_PRIORITY.get(b, 99))
    badge_cls = {"重要": "badge-important", "热门": "badge-hot", "实用": "badge-useful"}
    spans = []
    if include_new and is_new:
        spans.append('<span class="badge badge-new">新增</span>')
    for b in ordered:
        spans.append(f'<span class="badge {badge_cls.get(b, "badge-other")}">{esc(b)}</span>')
    if not spans:
        return ""
    return '<span class="badges">' + "".join(spans) + "</span>"


def item_priority(badges: list[str]) -> int:
    """仅按重点标签（重要/热门/实用）计算优先级，忽略【新增】。"""
    hot = [b for b in badges if b in BADGE_PRIORITY]
    if not hot:
        return 999
    return min(BADGE_PRIORITY.get(b, 99) for b in hot)


def data_tags_attr(badges: list[str]) -> str:
    cls_map = {"重要": "important", "热门": "hot", "实用": "useful", "新增": "new"}
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
        items.append((title, "\n".join(x.strip() for x in body if x.strip())))
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
            sections[sec] = sort_items_deals(merged)
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


def extract_image_url(body: str) -> tuple[str | None, str]:
    """抽出并剥掉「配图：URL」，返回 (url, 新正文)。"""
    m = IMAGE_LINE_RE.search(body)
    if not m:
        return None, body
    url = strip_url_punct(m.group(1))
    new_body = (body[: m.start()] + " " + body[m.end() :]).strip()
    return url, new_body


def extract_url_and_fields(body: str) -> tuple[str | None, str | None, str | None, str, dict[str, str]]:
    """返回 (url, risk, orig_title, desc, fields)。配图行会被剥掉，不进入摘要。"""
    _, body = extract_image_url(body)
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
    desc = re.sub(r"\s+", " ", desc).strip("。;； ").strip()
    return url, risk, orig_title, desc, fields


# ── 薅羊毛专用：上架时间 / 用途 解析 ────────────────────────────────────────

LISTING_TIME_LINE_RE = re.compile(r"(?:^|\n)\s*上架[：:]\s*(\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2})?)\s*(?:\n|$)")
USE_LINE_RE = re.compile(r"(?:^|\n)\s*用途[：:]\s*(.+?)(?=\n|$)")


def extract_listing_time(body: str) -> tuple[datetime | None, str]:
    """从正文提取「上架：YYYY-MM-DD HH:MM」，返回 (datetime或None, 剥掉上架行后的正文)。"""
    m = LISTING_TIME_LINE_RE.search(body)
    if not m:
        return None, body
    time_str = m.group(1).strip()
    new_body = (body[: m.start()] + body[m.end() :]).strip()
    try:
        if len(time_str) == 10:
            time_str += " 00:00"
        dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M").replace(tzinfo=BJ)
        return dt, new_body
    except ValueError:
        return None, new_body


def extract_use_line(body: str) -> tuple[str | None, str]:
    """从正文提取「用途：…」行，返回 (用途文字或None, 剥掉后的正文)。"""
    m = USE_LINE_RE.search(body)
    if not m:
        return None, body
    use_text = m.group(1).strip()
    new_body = (body[: m.start()] + body[m.end() :]).strip()
    return use_text, new_body


def sort_items_deals(items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """薅羊毛专用排序（替换 sort_items_by_badge for deals）：
    - 带【新增】的全部在前（新增间按上架时间新→旧；无上架时间的新增在有时间的新增之后，保持稿内相对顺序）
    - 其余条目按上架时间新→旧；无上架时间的保持稿内相对顺序，排在有时间的非新增之后
    """
    new_with_time: list[tuple[datetime, int, tuple[str, str]]] = []
    new_without_time: list[tuple[int, tuple[str, str]]] = []
    rest_with_time: list[tuple[datetime, int, tuple[str, str]]] = []
    rest_without_time: list[tuple[int, tuple[str, str]]] = []

    for idx, item in enumerate(items):
        raw_title, body = item
        badges, _ = extract_badges(raw_title)
        is_new = "新增" in badges
        listing_time, _ = extract_listing_time(body)
        if is_new:
            if listing_time is not None:
                new_with_time.append((listing_time, idx, item))
            else:
                new_without_time.append((idx, item))
        else:
            if listing_time is not None:
                rest_with_time.append((listing_time, idx, item))
            else:
                rest_without_time.append((idx, item))

    # 新增有时间的：按时间新→旧（降序）
    new_with_time.sort(key=lambda x: (-x[0].timestamp(), x[1]))
    # 其余有时间的：按时间新→旧（降序）
    rest_with_time.sort(key=lambda x: (-x[0].timestamp(), x[1]))

    result: list[tuple[str, str]] = []
    for _t, _i, item in new_with_time:
        result.append(item)
    for _i, item in new_without_time:
        result.append(item)
    for _t, _i, item in rest_with_time:
        result.append(item)
    for _i, item in rest_without_time:
        result.append(item)
    return result


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



# ── 资讯配图（本地下载 + 压缩）──────────────────────────────────────────────

def cleanup_old_images() -> None:
    """只清理 img/ 下本程序命名且日期早于 7 天的文件。"""
    if not IMG_DIR.is_dir():
        return
    cutoff = datetime.now(BJ).date() - timedelta(days=7)
    for path in IMG_DIR.iterdir():
        if not path.is_file():
            continue
        m = IMG_NAME_RE.match(path.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def _existing_image(short_hash: str) -> Path | None:
    if not IMG_DIR.is_dir():
        return None
    for path in IMG_DIR.iterdir():
        if not path.is_file():
            continue
        m = IMG_NAME_RE.match(path.name)
        if m and m.group(2) == short_hash:
            return path
    return None


def _compress_image_bytes(raw: bytes) -> tuple[bytes, str] | None:
    """压缩到宽 ≤800，尽量 ≤150KB。失败返回 None。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        return None
    try:
        if im.mode == "P":
            im = im.convert("RGBA") if "transparency" in im.info else im.convert("RGB")
        if im.width > 800:
            height = max(1, int(im.height * 800 / im.width))
            im = im.resize((800, height), Image.Resampling.LANCZOS)

        def encode(image: Image.Image, fmt: str, **kwargs) -> bytes:
            buf = io.BytesIO()
            image.save(buf, format=fmt, **kwargs)
            return buf.getvalue()

        limit = 150 * 1024
        if im.mode in ("RGBA", "LA"):
            chosen = None
            ext = "webp"
            for quality in (80, 70, 60, 50, 40):
                data = encode(im, "WEBP", quality=quality, method=6)
                chosen, ext = data, "webp"
                if len(data) <= limit:
                    break
            try:
                png = encode(im, "PNG", optimize=True)
                if chosen is None or len(png) < len(chosen):
                    chosen, ext = png, "png"
            except Exception:
                pass
            return (chosen, ext) if chosen else None

        rgb = im.convert("RGB")
        chosen = None
        for quality in (85, 75, 65, 55, 45, 35):
            data = encode(rgb, "JPEG", quality=quality, optimize=True)
            chosen = data
            if len(data) <= limit:
                break
        return (chosen, "jpg") if chosen else None
    except Exception:
        return None


def fetch_and_store_image(url: str) -> Path | None:
    """下载并压缩到 img/。任何失败返回 None，不抛给调用方。"""
    if not url or not url.startswith(("http://", "https://")):
        return None
    short_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    existing = _existing_image(short_hash)
    if existing:
        return existing
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": IMG_USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        if not raw:
            return None
        compressed = _compress_image_bytes(raw)
        if not compressed:
            return None
        data, ext = compressed
        day = datetime.now(BJ).strftime("%Y-%m-%d")
        dest = IMG_DIR / f"{day}-{short_hash}.{ext}"
        dest.write_bytes(data)
        return dest
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    except Exception:
        return None


def img_src_for(out_path: Path | None, img_path: Path) -> str:
    base = (out_path.resolve().parent if out_path else (ROOT / "news"))
    try:
        rel = img_path.resolve().relative_to(base, walk_up=True)
    except TypeError:
        # 极旧 Python 无 walk_up 时的兜底（本环境是 3.13）
        import os
        rel = Path(os.path.relpath(img_path.resolve(), start=base))
    return rel.as_posix()


def make_item_id(sec: str, raw_title: str, body: str) -> str:
    img_url, body2 = extract_image_url(body)
    url, _, _, _, _ = extract_url_and_fields(body2 if img_url else body)
    basis = f"{sec}|{raw_title}|{url or ''}"
    return "item-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def region_label(sub: str) -> str:
    """用户指定用词：国内 / 国际（不用「国外」）。"""
    return "国际" if sub == "国际" else "国内"

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


def render_item_news(
    raw_title: str,
    body: str,
    sec: str,
    sub: str,
    item_id: str,
    out_path: Path | None = None,
) -> str:
    """渲染资讯条目 HTML 片段（混排卡片，无封面 feat-item）。"""
    badges, clean_title = extract_badges(raw_title)
    img_url, body_wo_img = extract_image_url(body)
    url, risk, orig_title, desc, fields = extract_url_and_fields(body_wo_img)
    dtags = data_tags_attr(badges)

    parts = [f'<article class="item" id="{esc(item_id)}"{dtags}>']

    if badges and img_url:
        local = fetch_and_store_image(img_url)
        if local is not None:
            src = img_src_for(out_path, local)
            parts.append(
                f'<img class="item-img" src="{esc(src)}" alt="{esc(clean_title)}">'
            )

    region = region_label(sub)
    rcls = "badge-intl" if region == "国际" else "badge-domestic"
    badge_bits = [f'<span class="badge {rcls}">{esc(region)}</span>']
    ordered = sorted(badges, key=lambda b: BADGE_PRIORITY.get(b, 99))
    badge_cls = {"重要": "badge-important", "热门": "badge-hot", "实用": "badge-useful"}
    for b in ordered:
        badge_bits.append(
            f'<span class="badge {badge_cls.get(b, "badge-other")}">{esc(b)}</span>'
        )
    parts.append('<span class="badges">' + "".join(badge_bits) + "</span>")

    if url:
        parts.append(
            f'<h4><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(clean_title)}</a></h4>'
        )
    else:
        parts.append(f"<h4>{esc(clean_title)}</h4>")

    if orig_title:
        parts.append(f'<p class="title-en">{esc(orig_title)}</p>')

    label, reason = parse_risk(risk)
    if label:
        parts.append(f'<span class="risk">{esc(label)}</span>')
        if reason:
            parts.append(f'<span class="risk-reason">{esc(reason)}</span>')

    if desc:
        parts.append(f'<p class="desc">{esc(desc)}</p>')

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
            f'<a class="open-link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">看原文</a>'
        )
    parts.append("</div>")
    parts.append("</article>")
    return "\n".join(parts)


def _merge_sorted_news_items(submap: dict) -> list[tuple[str, str, str]]:
    """国内条目（原序）再接国际条目，再按标签稳定排序。返回 (title, body, sub)。"""
    merged: list[tuple[str, str, str]] = []
    for sub in ("国内", "国际"):
        for raw_title, body in submap.get(sub) or []:
            merged.append((raw_title, body, sub))

    def key_fn(item_with_index: tuple[int, tuple[str, str, str]]) -> tuple[int, int]:
        idx, (raw_title, _body, _sub) = item_with_index
        badges, _ = extract_badges(raw_title)
        return (item_priority(badges), idx)

    indexed = list(enumerate(merged))
    indexed.sort(key=key_fn)
    return [item for _, item in indexed]


def _render_hot_zone(data: dict) -> str:
    """今日热门：按分类列出带标签条目，每类最多 3 条；无标签分类不出现。"""
    groups: list[str] = []
    for sec in NEWS_SECTIONS:
        submap = data.get("sections", {}).get(sec) or {}
        tagged: list[tuple[int, int, str, str, str, list[str], str]] = []
        # 收集顺序：国内再国际，便于同优先级稳定
        n = 0
        for sub in ("国内", "国际"):
            for raw_title, body in submap.get(sub) or []:
                badges, clean = extract_badges(raw_title)
                if badges:
                    tagged.append((item_priority(badges), n, raw_title, body, sub, badges, clean))
                    n += 1
        tagged.sort(key=lambda t: (t[0], t[1]))
        tagged = tagged[:3]
        if not tagged:
            continue
        block = [f'<div class="hot-group">', f'<h3 class="hot-sec">{esc(sec)}</h3>', '<ul class="hot-list">']
        for _prio, _n, raw_title, body, _sub, badges, clean in tagged:
            iid = make_item_id(sec, raw_title, body)
            bhtml = badges_html(badges)
            block.append(
                f'<li><a class="hot-link" href="#{esc(iid)}">{bhtml}'
                f'<span class="hot-text">{esc(clean)}</span></a></li>'
            )
        block.append("</ul></div>")
        groups.append("\n".join(block))
    if not groups:
        return '<div id="hot" class="hot-anchor"></div>'
    return (
        '<section id="hot" class="hot-zone">\n'
        '<h2 class="hot-title">今日热门</h2>\n' + "\n".join(groups) + "\n</section>"
    )


def render_news_content(data: dict, out_path: Path | None = None) -> tuple[str, str, str, str]:
    """返回 (content_html, sec_nav_html, pill_html_str, hot_html)。"""
    pill = pill_html(data.get("status"), data["updated"], data.get("failed", False))

    if data["failed"]:
        return (
            '<p class="fail-panel">本次刷新失败</p>',
            "",
            pill,
            '<div id="hot" class="hot-anchor"></div>',
        )

    cleanup_old_images()
    hot = _render_hot_zone(data)

    sec_links = []
    for sec in NEWS_SECTIONS:
        sid = f"sec-{sec}"
        sec_links.append(f'<a href="#{sid}">{esc(sec)}</a>')
    sec_nav = '<nav class="sec-nav">' + "".join(sec_links) + "</nav>"

    parts = ['<div class="sections">']
    for sec in NEWS_SECTIONS:
        submap = data.get("sections", {}).get(sec) or {}
        items = _merge_sorted_news_items(submap)
        sid = f"sec-{sec}"
        channel_label = _SEC_CHANNEL.get(sec, sec + " Channel")
        parts.append(f'<section id="{sid}">')
        parts.append(f'<div class="section-header">{esc(channel_label)}</div>')

        if not items:
            parts.append('<p class="empty">暂无条目</p>')
            parts.append("</section>")
            continue

        tagged = [it for it in items if extract_badges(it[0])[0]]
        if tagged:
            visible, hidden = tagged, [it for it in items if not extract_badges(it[0])[0]]
        else:
            visible, hidden = items[:3], items[3:]

        parts.append('<div class="item-grid">')
        for raw_title, body, sub in visible:
            iid = make_item_id(sec, raw_title, body)
            parts.append(render_item_news(raw_title, body, sec, sub, iid, out_path))
        parts.append("</div>")

        if hidden:
            n = len(hidden)
            parts.append("<details class=\"more-box\">")
            if re.fullmatch(r"[A-Za-z0-9]+", sec):
                more_label = f"更多 {esc(sec)} 资讯（{n}）"
            else:
                more_label = f"更多{esc(sec)}资讯（{n}）"
            parts.append(f"<summary>{more_label}</summary>")
            parts.append('<div class="item-grid">')
            for raw_title, body, sub in hidden:
                iid = make_item_id(sec, raw_title, body)
                parts.append(render_item_news(raw_title, body, sec, sub, iid, out_path))
            parts.append("</div>")
            parts.append('<button type="button" class="collapse-btn">收起</button>')
            parts.append("</details>")

        parts.append("</section>")

    parts.append("</div>")
    return "\n".join(parts), sec_nav, pill, hot


# ── 薅羊毛页渲染 ──────────────────────────────────────────────────────────────

_CAT_CLS = {
    "白嫖": "free",
    "限免": "limited",
    "低价": "deal",
    "币圈空投": "airdrop",
    "NFT": "nft",
}


def render_item_deals(
    raw_title: str,
    body: str,
    sec: str,
    is_feat: bool = False,
    item_id: str = "",
    out_path: Path | None = None,
) -> str:
    """渲染薅羊毛条目 HTML 片段。"""
    badges, clean_title = extract_badges(raw_title)
    hot_badges = extract_hot_badges(badges)  # 重要/热门/实用（不含新增）

    # 剥掉新字段：用途、上架时间（配图在 extract_url_and_fields 里剥）
    use_text, body = extract_use_line(body)
    listing_dt, body = extract_listing_time(body)

    img_url, body_wo_img = extract_image_url(body)
    url, risk, orig_title, desc, fields = extract_url_and_fields(body_wo_img)
    dtags = data_tags_attr(badges)
    cat_data = f' data-cat="{esc(sec)}"'

    card_cls = "feat-item" if is_feat else "item"
    id_attr = f' id="{esc(item_id)}"' if item_id else ""
    parts = [f'<article class="{card_cls}"{id_attr}{dtags}{cat_data}>']

    # 配图：只有带重点标签（非仅新增）的条目才显示
    if hot_badges and img_url:
        local = fetch_and_store_image(img_url)
        if local is not None:
            src = img_src_for(out_path, local)
            parts.append(
                f'<img class="item-img" src="{esc(src)}" alt="{esc(clean_title)}">'
            )

    # 栏目标签
    cat_cls = _CAT_CLS.get(sec, "deal")
    parts.append(f'<div class="cat-tag {cat_cls}">{esc(sec)}</div>')

    # 重点徽章（含【新增】）
    bhtml = badges_html(badges, include_new=True)
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

    # 用途行（在原文标题之后、价格/摘要之前）
    if use_text:
        parts.append(
            f'<p class="item-use"><span class="item-use-label">用途</span>{esc(use_text)}</p>'
        )

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

    # 底部：来源 + 上架时间 + 领取/查看
    source = fields.get("来源", "")
    listing_short = ""
    if listing_dt is not None:
        listing_short = "上架 " + listing_dt.strftime("%m-%d %H:%M")
    source_line = ""
    if source and listing_short:
        source_line = source + " · " + listing_short
    elif source:
        source_line = source
    elif listing_short:
        source_line = listing_short

    parts.append('<div class="item-foot">')
    if source_line:
        parts.append(f'<span class="source-name">{esc(source_line)}</span>')
    if url:
        btn_txt = "领取 / 查看 →"
        parts.append(
            f'<a class="open-link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">{btn_txt}</a>'
        )
    parts.append("</div>")

    parts.append("</article>")
    return "\n".join(parts)


def _deals_item_ids(data: dict) -> dict[tuple[str, int], str]:
    """为每条薅羊毛条目生成稳定且唯一的卡片 id：key=(板块, 板块内排序后下标)。
    热门区与卡片共用，保证 href 与 id 一一对应；重复时追加 -2、-3。"""
    ids: dict[tuple[str, int], str] = {}
    seen: dict[str, int] = {}
    for sec in DEALS_SECTIONS:
        for idx, (raw_title, body) in enumerate(data.get("sections", {}).get(sec) or []):
            base = make_item_id(sec, raw_title, body)
            seen[base] = seen.get(base, 0) + 1
            ids[(sec, idx)] = base if seen[base] == 1 else f"{base}-{seen[base]}"
    return ids


def _render_hot_zone_deals(data: dict, ids: dict[tuple[str, int], str] | None = None) -> str:
    """今日热门（薅羊毛版）：按板块分组，各板块最多 5 条，无重点条目的板块不显示。
    进入条件：带【重要】【热门】【实用】（【新增】单独不算，但叠加时仍显示徽章）。
    组内排序（TASK3 §1.1）：【重要】>【热门】>【实用】；同档按上架时间新→旧；
    无上架时间的保持稿内相对顺序，排在同档有时间者之后。
    """
    groups: list[str] = []
    for sec in DEALS_SECTIONS:
        items = data.get("sections", {}).get(sec) or []
        tagged: list[tuple[int, int, float, str, str, list[str], str]] = []
        n = 0
        for raw_title, body in items:
            badges, clean = extract_badges(raw_title)
            hot = extract_hot_badges(badges)
            if not hot:
                n += 1
                continue
            listing_dt, _ = extract_listing_time(body)
            # 时间戳：有时间用负值（越大越新→越小越前），无时间用很大正数（后排）
            ts = -listing_dt.timestamp() if listing_dt else float("inf")
            tagged.append((item_priority(badges), n, ts, raw_title, body, badges, clean))
            n += 1
        # 重要>热门>实用；同档：有上架时间者新→旧（ts 为负时间戳），无时间者(inf)在后，再按稿内下标 n 稳定
        # 注意 n 是 sort_items_deals 之后的板块内顺序，而稿内相对顺序保持不变（无时间条目在其中相对顺序未变）
        tagged.sort(key=lambda t: (t[0], t[2], t[1]))
        tagged = tagged[:5]
        if not tagged:
            continue
        block = [
            f'<div class="hot-group">',
            f'<h3 class="hot-sec">{esc(sec)}</h3>',
            '<ul class="hot-list">',
        ]
        for _prio, _n, _ts, raw_title, body, badges, clean in tagged:
            iid = (ids or {}).get((sec, _n)) or make_item_id(sec, raw_title, body)
            bhtml = badges_html(badges, include_new=True)
            block.append(
                f'<li><a class="hot-link" href="#{esc(iid)}" data-cat="{esc(sec)}">{bhtml}'
                f'<span class="hot-text">{esc(clean)}</span></a></li>'
            )
        block.append("</ul></div>")
        groups.append("\n".join(block))

    if not groups:
        return '<div id="hot" class="hot-anchor"></div>'
    return (
        '<section id="hot" class="hot-zone">\n'
        '<h2 class="hot-title">今日热门</h2>\n' + "\n".join(groups) + "\n</section>"
    )


def render_deals_content(data: dict, out_path: Path | None = None) -> tuple[str, str, str, str]:
    """返回 (content_html, tabs_html, pill_html_str, hot_html)。"""
    pill = pill_html(data.get("status"), data["updated"], data.get("failed", False))

    if data["failed"]:
        return '<p class="fail-panel">本次刷新失败</p>', "", pill, '<div id="hot" class="hot-anchor"></div>'

    cleanup_old_images()
    ids = _deals_item_ids(data)
    hot = _render_hot_zone_deals(data, ids)

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

        # 封面任务（全局最多一次，第一个带重点标签条目——新增不算）
        start_idx = 0
        if not feat_done and items:
            first_badges, _ = extract_badges(items[0][0])
            first_hot = extract_hot_badges(first_badges)
            if first_hot:
                raw_title, body = items[0]
                iid = ids[(sec, 0)]
                parts.append(render_item_deals(raw_title, body, sec, is_feat=True, item_id=iid, out_path=out_path))
                start_idx = 1
                feat_done = True

        remaining = items[start_idx:]
        if remaining:
            parts.append('<div class="board-grid">')
            for off, (raw_title, body) in enumerate(remaining):
                iid = ids[(sec, start_idx + off)]
                parts.append(render_item_deals(raw_title, body, sec, item_id=iid, out_path=out_path))
            parts.append("</div>")
        elif start_idx == 0:
            parts.append('<p class="empty">暂无条目</p>')

        parts.append("</section>")

    parts.append("</div>")
    return "\n".join(parts), tabs_html, pill, hot


# ── 筛选 JS ───────────────────────────────────────────────────────────────────

FILTER_JS = """<script>
(function(){
  var tabs=document.getElementById('tabs');
  var board=document.getElementById('board');
  if(!tabs||!board) return;
  var items=[].slice.call(board.querySelectorAll('[data-cat]'));
  var currentCat='all';
  tabs.addEventListener('click',function(e){
    var b=e.target;
    if(b.tagName!=='BUTTON') return;
    [].forEach.call(tabs.querySelectorAll('button'),function(x){x.classList.toggle('on',x===b);});
    currentCat=b.dataset.cat;
    items.forEach(function(t){
      t.style.display=(currentCat==='all'||t.dataset.cat===currentCat)?'':'none';
    });
  });
  // 热门区点击：若目标卡片被 tab 筛选隐藏，先切回「全部任务」再跳转
  var hot=document.getElementById('hot');
  if(hot){
    hot.addEventListener('click',function(e){
      var a=e.target.closest('.hot-link');
      if(!a) return;
      var href=a.getAttribute('href');
      if(!href||href[0]!=='#') return;
      var targetEl=document.getElementById(href.slice(1));
      if(!targetEl) return;
      if(targetEl.style.display==='none'){
        // 切回全部
        [].forEach.call(tabs.querySelectorAll('button'),function(x){
          x.classList.toggle('on',x.dataset.cat==='all');
        });
        currentCat='all';
        items.forEach(function(t){ t.style.display=''; });
      }
    });
  }
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
        content, sec_nav, pill, hot = render_news_content(data, out_path)
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
                "hot": hot,
                "sec_nav": sec_nav,
                "content": content,
            },
        )

    # deals
    content, tabs, pill, hot = render_deals_content(data, out_path)
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
            "hot": hot,
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
