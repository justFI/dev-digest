#!/usr/bin/env python3
"""Collect dev news from RSS feeds and publish HTML digest to docs/."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
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


def fetch_feed(url: str, label: str) -> list[dict]:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=25,
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] {label}: {exc}", file=sys.stderr)
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
        if len(describe) > 600:
            describe = describe[:597] + "..."

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


def render_html(items_by_cat: dict[str, list[dict]], config: dict) -> str:
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    total = sum(len(v) for v in items_by_cat.values())

    cat_meta = {c["id"]: c["name"] for c in config["categories"]}

    sections_html = []
    for cat in config["categories"]:
        cid = cat["id"]
        items = items_by_cat.get(cid, [])
        if not items:
            continue

        cards = []
        for it in items:
            pub = it.get("published")
            pub_str = pub.strftime("%Y-%m-%d %H:%M UTC") if pub else "—"
            media_html = ""
            if it.get("media"):
                thumbs = []
                for i, murl in enumerate(it["media"][:4]):
                    esc = html.escape(murl)
                    if re.search(r"\.(jpg|jpeg|png|gif|webp|svg)(\?|$)", murl, re.I):
                        thumbs.append(
                            f'<a href="{esc}" target="_blank" rel="noopener">'
                            f'<img src="{esc}" alt="media-{i}" loading="lazy" /></a>'
                        )
                    else:
                        thumbs.append(f'<a href="{esc}" target="_blank" rel="noopener">{esc[:60]}…</a>')
                media_html = f'<div class="media">{"".join(thumbs)}</div>'
            else:
                media_html = '<div class="media empty">暂无媒体资源</div>'

            cards.append(
                f"""
        <article class="card" data-category="{html.escape(cid)}">
          <span class="badge">{html.escape(cat_meta.get(cid, cid))}</span>
          <h3 class="title"><a href="{html.escape(it['origin_url'])}" target="_blank" rel="noopener">{html.escape(it['title'])}</a></h3>
          <p class="meta">来源：{html.escape(it.get('source_label', ''))} · {html.escape(pub_str)}</p>
          <p class="describe">{html.escape(it.get('describe') or '（无摘要）')}</p>
          <div class="ai-box">
            <strong>AI 总结</strong>
            <p>{html.escape(it.get('ai_summary', ''))}</p>
          </div>
          {media_html}
          <p class="origin"><a href="{html.escape(it['origin_url'])}" target="_blank" rel="noopener">原文链接 →</a></p>
        </article>"""
            )

        sections_html.append(
            f"""
      <section id="cat-{html.escape(cid)}">
        <h2>{html.escape(cat['name'])} <span class="count">{len(items)}</span></h2>
        <div class="grid">{''.join(cards)}</div>
      </section>"""
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>开发资讯日报 · {date_str}</title>
  <meta name="description" content="AI、iOS、Android、前端、后端等领域每日开发技巧与行业干货聚合" />
  <link rel="stylesheet" href="style.css" />
</head>
<body>
  <header class="hero">
    <h1>开发资讯日报</h1>
    <p class="subtitle">AI · iOS · Android · 前端 · 后端 · 行业干货</p>
    <p class="updated">更新于 {now.strftime("%Y-%m-%d %H:%M UTC")} · 共 {total} 条</p>
    <nav class="toc">
      {"".join(f'<a href="#cat-{html.escape(c["id"])}">{html.escape(c["name"])}</a>' for c in config["categories"])}
    </nav>
  </header>
  <main>
    {''.join(sections_html) if sections_html else '<p class="empty">今日暂无新资讯，请稍后重试。</p>'}
  </main>
  <footer>
    <p>由 <a href="https://github.com/justFI/dev-digest">dev-digest</a> 自动采集 · 
    <a href="https://github.com/justFI/dev-digest/actions">GitHub Actions</a> 每日更新</p>
  </footer>
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
