# -*- coding: utf-8 -*-
"""第二大脑·新鲜事：从 RSS 抓取新闻，按知识库关键词匹配，存入 records。

纯标准库实现，不引入新依赖。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

# 数据目录跟主程序一致：优先 IP_DATA_DIR，其次包内 app/.runtime
_BASE = Path(__file__).resolve().parent
_DATA_DIR = Path(os.getenv("IP_DATA_DIR") or (_BASE / ".runtime"))
_DB_PATH = _DATA_DIR / "workbench.sqlite3"

# 只保留最近这么多条新闻，避免数据库和备份包无限膨胀
NEWS_KEEP_LIMIT = 200

# 可用的 RSS 源（已实测 2026-11-10）
SOURCES = [
    ("少数派", "https://sspai.com/feed"),
    ("极客公园", "http://www.geekpark.net/rss"),
    ("爱范儿", "https://www.ifanr.com/feed"),
    ("cnBeta", "https://www.cnbeta.com.tw/backend.php"),
]

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_DB_PATH))
    con.row_factory = sqlite3.Row
    return con


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _extract_text_from_db(days: int = 365) -> str:
    """从知识库全部记录提取文本，用于关键词挖掘。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    texts: list[str] = []
    with _connect() as con:
        # knowledge 认知
        for row in con.execute(
            "SELECT data FROM records WHERE kind='knowledge' AND updated_at>=?", (cutoff,)
        ):
            try:
                d = json.loads(row["data"])
                texts.append(d.get("title", ""))
                texts.append(d.get("claim", "")[:500])
                texts.append(d.get("body", "")[:500])
            except Exception:
                pass
        # 选题
        for row in con.execute(
            "SELECT data FROM records WHERE kind='topic_pool' AND created_at>=?", (cutoff,)
        ):
            try:
                d = json.loads(row["data"])
                texts.append(d.get("title", ""))
                texts.append(d.get("angle", "")[:200])
            except Exception:
                pass
        # 项目文案
        for row in con.execute(
            "SELECT data FROM records WHERE kind='project' AND updated_at>=?", (cutoff,)
        ):
            try:
                d = json.loads(row["data"])
                texts.append(d.get("title", ""))
                texts.append(d.get("script", "")[:500])
            except Exception:
                pass
    return "\n".join(texts)


def _extract_keywords(text: str, top_n: int = 10) -> list[str]:
    """极简关键词提取：2-4 字中文词频统计，过滤停用词。"""
    stop = {
        "的", "了", "在", "是", "我", "你", "他", "她", "它", "们", "这", "那", "有", "就",
        "都", "而", "及", "与", "或", "一个", "没有", "什么", "怎么", "如何", "因为", "所以",
        "但是", "如果", "虽然", "不过", "只是", "就是", "这个", "那个", "这些", "那些",
        "自己", "我们", "你们", "他们", "可以", "应该", "需要", "进行", "通过", "根据",
        "对于", "关于", "一种", "一些", "非常", "比较", "更加", "已经", "正在", "将要",
        "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "at", "for",
        # 模板词
        "原文", "中译", "怎么用", "机制", "为什么", "原话", "一句话主张", "边界", "三观",
        "为什么成立", "关联", "换到今天要", "的人", "的东西", "来的", "不是", "主播自述",
        "时间", "数据", "补充", "说明", "更正", "用词", "版本", "记录", "提炼", "本质",
        # 工作流词汇
        "链路", "验收", "布稿", "字幕", "净剪", "非本人", "发布稿", "完整版",
    }
    # 提取 2-5 字中文词
    words = re.findall(r"[\u4e00-\u9fff]{2,5}", text)
    freq: dict[str, int] = {}
    for w in words:
        if w in stop:
            continue
        if re.fullmatch(r"\d+", w):
            continue
        freq[w] = freq.get(w, 0) + 1
    # 只保留出现 >=2 次的词，过滤单次噪音
    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [w for w, c in sorted_words if c >= 2][:top_n]


def _clean(text: str) -> str:
    """压掉换行与多余空白，RSS 标题常带换行缩进。"""
    return " ".join((text or "").split())


def _fetch_rss(url: str, source_name: str) -> list[dict]:
    """抓取单个 RSS 源，返回 [{title, url, summary, published, source}]."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = resp.read()
    root = ET.fromstring(data)
    items = root.findall(".//item")
    if not items:
        # Atom 格式
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//atom:entry", ns)
        results = []
        for item in items:
            title_el = item.find("atom:title", ns)
            link_el = item.find("atom:link", ns)
            summary_el = item.find("atom:summary", ns) or item.find("atom:content", ns)
            pub_el = item.find("atom:published", ns) or item.find("atom:updated", ns)
            results.append({
                "title": title_el.text if title_el is not None else "",
                "url": link_el.get("href", "") if link_el is not None else "",
                "summary": (summary_el.text or "")[:200] if summary_el is not None else "",
                "published": pub_el.text if pub_el is not None else "",
                "source": source_name,
            })
        return results
    results = []
    for item in items:
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        pub_el = item.find("pubDate") or item.find("date")
        results.append({
            "title": title_el.text if title_el is not None else "",
            "url": link_el.text if link_el is not None else "",
            "summary": (desc_el.text or "")[:200] if desc_el is not None else "",
            "published": pub_el.text if pub_el is not None else "",
            "source": source_name,
        })
    return results


def _match_keywords(news_items: list[dict], keywords: list[str]) -> list[dict]:
    """按关键词匹配新闻，返回 matched_keywords 字段。"""
    matched = []
    for item in news_items:
        text = item["title"] + " " + item["summary"]
        hits = [k for k in keywords if k in text]
        if hits:
            item["matched_keywords"] = hits
            item["match_count"] = len(hits)
            matched.append(item)
    # 按匹配数量降序
    matched.sort(key=lambda x: x["match_count"], reverse=True)
    return matched


def _save_news(items: list[dict]) -> int:
    """去重后存入 records，kind='news'。返回新增条数。"""
    saved = 0
    with _connect() as con:
        for item in items:
            url = item.get("url", "")
            if not url:
                continue
            # 按 url 去重
            row = con.execute(
                "SELECT id FROM records WHERE kind='news' AND json_extract(data,'$.url')=?",
                (url,),
            ).fetchone()
            if row:
                continue
            rid = f"news-{int(time.time()*1000)}-{saved}"
            data = {
                "title": item["title"],
                "url": url,
                "summary": item.get("summary", ""),
                "source": item.get("source", ""),
                "published": item.get("published", ""),
                "matched_keywords": item.get("matched_keywords", []),
                "fetched_at": _now_iso(),
            }
            con.execute(
                "INSERT INTO records(id, kind, data, created_at, updated_at, revision) VALUES(?,?,?,?,?,1)",
                (rid, "news", json.dumps(data, ensure_ascii=False), _now_iso(), _now_iso()),
            )
            saved += 1
        # 只留最近 NEWS_KEEP_LIMIT 条，删掉更早的，避免库和备份包无限长
        con.execute(
            "DELETE FROM records WHERE kind='news' AND id NOT IN ("
            "SELECT id FROM records WHERE kind='news' ORDER BY updated_at DESC, id DESC LIMIT ?)",
            (NEWS_KEEP_LIMIT,),
        )
        con.commit()
    return saved


def refresh_news(days: int = 7, max_keywords: int = 10, max_items: int = 10, custom_keywords: list[str] | None = None) -> dict:
    """主入口：抓取 -> 匹配 -> 存储 -> 返回结果摘要。

    优先使用用户自定义关键词；无自定义时从知识库自动提取。
    """
    # 1. 提取关键词
    if custom_keywords:
        keywords = [k.strip() for k in custom_keywords if k.strip()][:max_keywords]
    else:
        text = _extract_text_from_db(days=days)
        keywords = _extract_keywords(text, top_n=max_keywords)
    if not keywords:
        return {"ok": False, "error": "没有可用的关键词。请在设置里填写赛道关键词，或先记几条认知。"}

    # 2. 抓取 RSS
    all_items: list[dict] = []
    errors: list[str] = []
    for name, url in SOURCES:
        try:
            items = _fetch_rss(url, name)
            all_items.extend(items)
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")

    # 3. 匹配
    matched = _match_keywords(all_items, keywords)
    top = matched[:max_items]

    # 4. 存储
    saved = _save_news(top)

    return {
        "ok": True,
        "keywords": keywords,
        "total_fetched": len(all_items),
        "matched": len(matched),
        "saved": saved,
        "errors": errors,
        "items": top,
    }


def list_news(limit: int = 20) -> list[dict]:
    """读取已存的新闻，按 created_at 倒序。"""
    with _connect() as con:
        rows = con.execute(
            "SELECT data FROM records WHERE kind='news' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in rows:
        try:
            item = json.loads(row["data"])
            item["title"] = _clean(item.get("title", ""))
            item["summary"] = _clean(item.get("summary", ""))
            result.append(item)
        except Exception:
            pass
    return result
