#!/usr/bin/env python3
"""Collect dev news from RSS feeds and publish HTML digest to docs/."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "sources.yaml"
DOCS_DIR = ROOT / "docs"
DATA_DIR = ROOT / "data"
MAX_AGE_HOURS = int(os.environ.get("DIGEST_MAX_AGE_HOURS", "48"))
MAX_PER_CATEGORY = int(os.environ.get("DIGEST_MAX_PER_CATEGORY", "12"))
FEED_DELAY_SEC = float(os.environ.get("DIGEST_FEED_DELAY_SEC", "1.5"))
REDDIT_DELAY_SEC = float(os.environ.get("DIGEST_REDDIT_DELAY_SEC", "5.0"))
USER_AGENT = "dev-digest-bot/1.0 (+https://github.com/justFI/dev-digest)"


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    return base.lower()


def item_fingerprint(title: str, link: str) -> str:
    key = f"{normalize_url(link)}|{title.strip().lower()[:120]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def parse_published(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            try:
                dt = date_parser.parse(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (ValueError, TypeError):
                pass
    return None


def extract_media(entry: dict, content_html: str) -> list[str]:
    media: list[str] = []
    seen: set[str] = set()

    def add(url: str | None) -> None:
        if not url or not url.startswith(("http://", "https://")):
            return
        if url in seen:
            return
        seen.add(url)
        media.append(url)

    if entry.get("media_thumbnail"):
        for m in entry["media_thumbnail"]:
            add(m.get("url"))
    if entry.get("media_content"):
        for m in entry["media_content"]:
            add(m.get("url"))
    if entry.get("enclosures"):
        for enc in entry["enclosures"]:
            if enc.get("type", "").startswith(("image/", "video/")):
                add(enc.get("href"))

    if content_html:
        soup = BeautifulSoup(content_html, "html.parser")
        for img in soup.find_all("img", src=True):
            add(img["src"])
        for video in soup.find_all("video", src=True):
            add(video["src"])

    return media[:8]


def strip_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(separator=" ")).strip()


def is_valid_describe(text: str) -> bool:
    if not text or len(text.strip()) < 8:
        return False
    if re.search(r"\{\{.*?\}\}", text):
        return False
    if text.strip() in ("[link] [comments]", "[removed]", "[deleted]"):
        return False
    return True


def feed_backoff_seconds(exc: Exception, attempt: int, url: str) -> int:
    """Longer waits for rate limits and Reddit throttling."""
    msg = str(exc).lower()
    if "429" in msg or "reddit.com" in url:
        return min(30, 8 + attempt * 6)
    return 2**attempt


def fetch_feed(url: str, label: str, retries: int = 3) -> list[dict]:
    last_exc: Exception | None = None
    parsed = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < retries - 1:
                wait = feed_backoff_seconds(exc, attempt, url)
                print(f"  [retry] {label} attempt {attempt + 2}/{retries} in {wait}s...", file=sys.stderr)
                time.sleep(wait)
    if parsed is None:
        print(f"  [warn] {label}: {last_exc}", file=sys.stderr)
        return []

    items: list[dict] = []
    for entry in parsed.entries[:30]:
        link = entry.get("link") or entry.get("id")
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue

        content_html = ""
        if entry.get("content"):
            content_html = entry["content"][0].get("value", "")
        elif entry.get("summary"):
            content_html = entry.get("summary", "")

        describe = strip_html(content_html) or strip_html(entry.get("summary", ""))
        if not is_valid_describe(describe):
            describe = strip_html(entry.get("title", ""))
        if len(describe) > 600:
            describe = describe[:597] + "..."
        if not is_valid_describe(describe):
            continue

        items.append(
            {
                "title": title,
                "describe": describe,
                "media": extract_media(entry, content_html),
                "origin_url": link,
                "published": parse_published(entry),
                "source_label": label,
            }
        )
    return items


def ai_summarize(title: str, describe: str, category_name: str) -> str:
    """Generate Chinese summary via OpenAI-compatible API or extractive fallback."""
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DIGEST_OPENAI_API_KEY")
    base_url = os.environ.get(
        "OPENAI_BASE_URL",
        os.environ.get("DIGEST_OPENAI_BASE_URL", "https://api.openai.com/v1"),
    )
    model = os.environ.get("OPENAI_MODEL", os.environ.get("DIGEST_OPENAI_MODEL", "gpt-4o-mini"))

    prompt = (
        f"你是资深技术编辑。请用 2-4 句简体中文总结下面这条「{category_name}」领域开发资讯，"
        "突出：技术要点、实践经验或难点解决思路。不要编造原文没有的信息。\n\n"
        f"标题：{title}\n\n正文摘要：{describe[:1200]}"
    )

    if api_key:
        try:
            resp = requests.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "你只输出简体中文摘要，不要标题、不要列表符号。",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 220,
                    "temperature": 0.3,
                },
                timeout=45,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content:
                return content
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] AI API failed, using fallback: {exc}", file=sys.stderr)

    return extractive_summary_zh(title, describe, category_name)


def extractive_summary_zh(title: str, describe: str, category_name: str) -> str:
    """Rule-based Chinese summary when no API key."""
    text = describe or title
    sentences = re.split(r"[.!?。！？\n]+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 12]
    core = "。".join(sentences[:2]) if sentences else text[:200]
    if len(core) > 180:
        core = core[:177] + "..."
    tags = []
    lower = (title + " " + describe).lower()
    keyword_map = {
        "教程": ["tutorial", "guide", "how to", "入门"],
        "开源": ["open source", "github", "release"],
        "性能": ["performance", "optimize", "benchmark"],
        "安全": ["security", "vulnerability", "cve"],
        "架构": ["architecture", "microservice", "scale"],
    }
    for zh, keys in keyword_map.items():
        if any(k in lower for k in keys):
            tags.append(zh)
    tag_str = f"（{'/'.join(tags[:2])}）" if tags else ""
    return f"【{category_name}】{title[:60]}{tag_str}。{core}"


def collect_all(config: dict) -> dict[str, list[dict]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    seen_fp: set[str] = set()
    by_category: dict[str, list[dict]] = {}

    for cat in config["categories"]:
        cat_id = cat["id"]
        cat_name = cat["name"]
        collected: list[dict] = []

        for feed in cat["feeds"]:
            url = feed["url"]
            label = feed.get("label", url)
            print(f"Fetching [{cat_id}] {label}...")
            for raw in fetch_feed(url, label):
                fp = item_fingerprint(raw["title"], raw["origin_url"])
                if fp in seen_fp:
                    continue
                pub = raw.get("published")
                if pub and pub < cutoff:
                    continue
                seen_fp.add(fp)
                collected.append({**raw, "category_id": cat_id, "category_name": cat_name})

            delay = REDDIT_DELAY_SEC if "reddit.com" in url else FEED_DELAY_SEC
            time.sleep(delay)

        collected.sort(
            key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        by_category[cat_id] = collected[:MAX_PER_CATEGORY]

    return by_category


def enrich_with_ai(items_by_cat: dict[str, list[dict]]) -> None:
    for items in items_by_cat.values():
        for item in items:
            print(f"  Summarizing: {item['title'][:50]}...")
            item["ai_summary"] = ai_summarize(
                item["title"],
                item["describe"],
                item["category_name"],
            )


CAT_ICONS: dict[str, str] = {
    "ai":       "🤖",
    "ios":      "🍎",
    "android":  "🤖",
    "frontend": "🎨",
    "backend":  "⚙️",
    "general":  "📰",
}

_SVG_CLOCK = (
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">'
    '<circle cx="8" cy="8" r="6.5"/><path d="M8 5v3.5l2 1.5"/></svg>'
)
_SVG_LINK = (
    '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">'
    '<path d="M6.5 3.5H4A2.5 2.5 0 0 0 4 8.5h2"/>'
    '<path d="M9.5 12.5H12A2.5 2.5 0 0 0 12 7.5h-2"/>'
    '<line x1="5.5" y1="8" x2="10.5" y2="8"/></svg>'
)
_SVG_AI = (
    '<svg viewBox="0 0 16 16" fill="currentColor">'
    '<path d="M8 1a1 1 0 0 1 1 1v1.268a5 5 0 1 1-2 0V2a1 1 0 0 1 1-1zm0 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"/>'
    '</svg>'
)
_SVG_ARROW = (
    '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8">'
    '<path d="M2 6h8M6 2l4 4-4 4"/></svg>'
)


def render_html(items_by_cat: dict[str, list[dict]], config: dict) -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    total = sum(len(v) for v in items_by_cat.values())
    cat_count = sum(1 for c in config["categories"] if items_by_cat.get(c["id"]))

    cat_meta = {c["id"]: c["name"] for c in config["categories"]}

    sections_html = []
    for cat in config["categories"]:
        cid = cat["id"]
        items = items_by_cat.get(cid, [])
        if not items:
            continue

        icon = CAT_ICONS.get(cid, "📌")
        cards = []
        for it in items:
            pub = it.get("published")
            pub_str = pub.strftime("%m-%d %H:%M") if pub else "—"

            # media
            has_img = False
            if it.get("media"):
                thumbs = []
                has_text_only = True
                for i, murl in enumerate(it["media"][:4]):
                    esc = html.escape(murl)
                    if re.search(r"\.(jpg|jpeg|png|gif|webp|svg)(\?|$)", murl, re.I):
                        thumbs.append(
                            f'<a href="{esc}" target="_blank" rel="noopener">'
                            f'<img src="{esc}" alt="media" loading="lazy" /></a>'
                        )
                        has_text_only = False
                        has_img = True
                    else:
                        thumbs.append(
                            f'<a href="{esc}" target="_blank" rel="noopener" title="{esc}">'
                            f'{esc[:55]}…</a>'
                        )
                media_cls = "media" + (" has-text" if has_text_only else "")
                media_html = f'<div class="{media_cls}">{"".join(thumbs)}</div>'
            else:
                media_html = ""

            ai_text = html.escape(it.get("ai_summary") or "")
            describe_text = html.escape(it.get("describe") or "（无摘要）")
            title_esc = html.escape(it["title"])
            url_esc = html.escape(it["origin_url"])
            src_esc = html.escape(it.get("source_label") or "")
            badge_name = html.escape(cat_meta.get(cid, cid))

            cards.append(
                f"""
        <article class="card" data-category="{cid}" data-title="{title_esc.lower()}" data-text="{describe_text[:120].lower()}">
          <div class="card-top">
            <span class="badge">{badge_name}</span>
            <span class="card-meta">{_SVG_CLOCK} {html.escape(pub_str)}</span>
          </div>
          <h3 class="title"><a href="{url_esc}" target="_blank" rel="noopener">{title_esc}</a></h3>
          <p class="describe">{describe_text}</p>
          <div class="ai-box">
            <div class="ai-label">{_SVG_AI} AI 总结</div>
            <p>{ai_text}</p>
          </div>
          {media_html}
          <p class="origin-url" title="原文链接">
            <span class="origin-label">Origin URL</span>
            <a href="{url_esc}" target="_blank" rel="noopener">{url_esc}</a>
          </p>
          <div class="card-footer">
            <span class="card-meta" style="margin-right:auto">{_SVG_LINK} {src_esc}</span>
            <a class="read-link" href="{url_esc}" target="_blank" rel="noopener">
              阅读原文 {_SVG_ARROW}
            </a>
          </div>
        </article>"""
            )

        sections_html.append(
            f"""
      <section id="cat-{cid}" data-section="{cid}">
        <div class="cat-header">
          <span class="cat-icon">{icon}</span>
          <h2>{html.escape(cat["name"])}</h2>
          <span class="count">{len(items)}</span>
        </div>
        <div class="grid" id="grid-{cid}">
          {''.join(cards)}
          <div class="no-results" id="nores-{cid}">该分类暂无匹配结果</div>
        </div>
      </section>"""
        )

    nav_pills = "".join(
        f'<button class="nav-pill" data-filter="{c["id"]}">'
        f'{CAT_ICONS.get(c["id"], "📌")} {html.escape(c["name"])}</button>'
        for c in config["categories"]
        if items_by_cat.get(c["id"])
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>开发资讯日报 · {date_str}</title>
  <meta name="description" content="AI、iOS、Android、前端、后端等领域每日开发技巧与行业干货聚合" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" />
  <link rel="stylesheet" href="style.css" />
</head>
<body>
  <div id="progress-bar"></div>

  <nav class="top-nav">
    <span class="brand">⚡ <span>Dev Digest</span></span>
    <div class="nav-pills">
      <button class="nav-pill active" data-filter="all">全部</button>
      {nav_pills}
    </div>
  </nav>

  <header class="hero">
    <div class="hero-eyebrow">每日自动采集</div>
    <h1>开发资讯日报</h1>
    <p class="subtitle">AI · iOS · Android · 前端 · 后端 · 行业干货</p>
    <p class="updated">
      🕐 更新于 {now.strftime("%Y-%m-%d %H:%M UTC")}
    </p>
  </header>

  <div class="stats-bar">
    <div class="stat-item">
      <div class="num">{total}</div>
      <div class="label">今日资讯</div>
    </div>
    <div class="stat-item">
      <div class="num">{cat_count}</div>
      <div class="label">技术领域</div>
    </div>
    <div class="stat-item">
      <div class="num">{date_str}</div>
      <div class="label">更新日期</div>
    </div>
  </div>

  <div class="search-wrap">
    <div class="search-box">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
        <circle cx="6.5" cy="6.5" r="4.5"/><path d="M10.5 10.5l3 3"/>
      </svg>
      <input type="search" id="search-input" placeholder="搜索标题或摘要…" autocomplete="off" />
    </div>
  </div>

  <main id="main-content">
    {''.join(sections_html) if sections_html else '<p class="empty">今日暂无新资讯，请稍后重试。</p>'}
  </main>

  <footer>
    <p>
      由 <a href="https://github.com/justFI/dev-digest" target="_blank" rel="noopener">dev-digest</a> 自动采集 ·
      <a href="https://github.com/justFI/dev-digest/actions" target="_blank" rel="noopener">GitHub Actions</a> 每日 UTC 01:00 更新
    </p>
  </footer>

  <script>
  (function () {{
    // Reading progress bar
    var bar = document.getElementById('progress-bar');
    window.addEventListener('scroll', function () {{
      var s = document.documentElement;
      var pct = s.scrollTop / (s.scrollHeight - s.clientHeight) * 100;
      bar.style.width = Math.min(pct, 100) + '%';
    }}, {{ passive: true }});

    // Category filter + search
    var pills = document.querySelectorAll('.nav-pill');
    var cards = document.querySelectorAll('.card');
    var sections = document.querySelectorAll('[data-section]');
    var searchInput = document.getElementById('search-input');
    var activeFilter = 'all';
    var searchQuery = '';

    function applyFilters() {{
      sections.forEach(function (sec) {{
        var sid = sec.dataset.section;
        var visible = 0;
        sec.querySelectorAll('.card').forEach(function (card) {{
          var catMatch = activeFilter === 'all' || card.dataset.category === activeFilter;
          var q = searchQuery.trim();
          var textMatch = !q ||
            card.dataset.title.includes(q) ||
            card.dataset.text.includes(q);
          if (catMatch && textMatch) {{
            card.classList.remove('hidden');
            visible++;
          }} else {{
            card.classList.add('hidden');
          }}
        }});
        var nores = document.getElementById('nores-' + sid);
        if (nores) nores.classList.toggle('visible', visible === 0);
        sec.style.display = (activeFilter !== 'all' && activeFilter !== sid) ? 'none' : '';
      }});
    }}

    pills.forEach(function (pill) {{
      pill.addEventListener('click', function () {{
        pills.forEach(function (p) {{ p.classList.remove('active'); }});
        pill.classList.add('active');
        activeFilter = pill.dataset.filter;
        applyFilters();
        if (activeFilter !== 'all') {{
          var target = document.getElementById('cat-' + activeFilter);
          if (target) target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
        }}
      }});
    }});

    searchInput.addEventListener('input', function () {{
      searchQuery = this.value.toLowerCase();
      applyFilters();
    }});
  }})();
  </script>
</body>
</html>"""


def save_outputs(items_by_cat: dict[str, list[dict]], config: dict, html_content: str) -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    (DOCS_DIR / "index.html").write_text(html_content, encoding="utf-8")

    archive_name = f"digest-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.json"
    serializable: dict[str, Any] = {}
    for cid, items in items_by_cat.items():
        serializable[cid] = []
        for it in items:
            row = dict(it)
            if row.get("published"):
                row["published"] = row["published"].isoformat()
            serializable[cid].append(row)

    (DATA_DIR / "latest.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "categories": serializable,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (DATA_DIR / archive_name).write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    print("Loading config...")
    config = load_config()
    print("Collecting feeds...")
    items = collect_all(config)
    print("Generating AI summaries...")
    enrich_with_ai(items)
    print("Rendering HTML...")
    html_out = render_html(items, config)
    save_outputs(items, config, html_out)
    total = sum(len(v) for v in items.values())
    print(f"Done. {total} items written to {DOCS_DIR / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
