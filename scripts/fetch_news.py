#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EconoScout · 新闻抓取器（零第三方依赖）

职责：从多个公开财经/科技 RSS 源抓取候选新闻，去重、按经济相关度排序，
      输出 data/raw-news.json，供 generate_brief.py 交给大模型解读。

设计原则：
  1. 多源容错——任何单个源失败都只记日志，不影响整体出报
  2. 只抓公开 RSS/页面，不碰登录/破解内容，来源均可回溯
  3. 仅用 Python 标准库，本地与 GitHub Actions 都免安装依赖
  4. 源清单集中在 SOURCES，随时可增删

网络提示：
  - GitHub Actions 运行在海外，Google News / Bing News RSS 正常可用
  - 国内网络对部分源有限制，脚本会自动跳过失败源，用可用的源出报

用法：
  python fetch_news.py            # 抓取并写入 data/raw-news.json
  python fetch_news.py --verbose  # 打印每个源的详细状态
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from html import unescape

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
OUT = os.path.join(DATA_DIR, "raw-news.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# ---------------------------------------------------------------- 数据源清单
# 每条：(来源名, 主题, URL)。{q} 会被关键词替换。
# 关键词覆盖用户既有素材库主题：物价、生产、资金、全球利率、产业价格。
KEYWORDS_ZH = ["中国经济 CPI PPI", "央行 逆回购 利率", "人民币 汇率 关税", "美联储 加息 通胀"]
KEYWORDS_EN = ["China economy inflation", "Federal Reserve rate decision", "global oil prices"]

SOURCES = []
for q in KEYWORDS_ZH:
    SOURCES.append(("GoogleNews", "宏观/物价",
        "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"))
    SOURCES.append(("BingNews", "宏观/物价",
        "https://www.bing.com/news/search?q=" + urllib.parse.quote(q) + "&format=rss"))
for q in KEYWORDS_EN:
    SOURCES.append(("GoogleNewsEN", "全球经济",
        "https://news.google.com/rss/search?q=" + urllib.parse.quote(q) + "&hl=en-US&gl=US&ceid=US:en"))
# 常驻源（不依赖关键词），国内网络上也能通，作为兜底
SOURCES += [
    ("36氪", "产业/科技", "https://www.36kr.com/feed"),
    ("爱范儿", "产业/科技", "https://www.ifanr.com/feed"),
    ("Yahoo财经", "全球经济", "https://finance.yahoo.com/news/rssindex"),
]


def log(msg, on=True):
    if on:
        print(msg, flush=True)


def fetch_url(url, timeout=15):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset)
        except (UnicodeDecodeError, LookupError):
            return raw.decode("utf-8", errors="replace")


def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", unescape(s)).strip()


def parse_rss(xml_text, source_name, topic):
    """解析 RSS 2.0 / Atom，返回条目列表。"""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    ns = {"a": "http://www.w3.org/2005/Atom"}
    nodes = root.findall(".//item") or root.findall(".//a:entry", ns)
    for node in nodes[:15]:
        title = node.findtext("title") or node.findtext("a:title", default="", namespaces=ns) or ""
        link = node.findtext("link") or ""
        if not link:
            le = node.find("a:link", ns)
            link = le.get("href", "") if le is not None else ""
        desc = node.findtext("description") or node.findtext("a:summary", default="", namespaces=ns) or ""
        pub = (node.findtext("pubDate") or node.findtext("a:published", default="", namespaces=ns)
               or node.findtext("a:updated", default="", namespaces=ns) or "")
        title = strip_html(title)
        if title and link.startswith("http"):
            items.append({
                "title": title, "url": link.strip(), "summary": strip_html(desc)[:280],
                "published_raw": pub.strip(), "source": source_name, "topic": topic,
            })
    return items


def norm_title(t):
    return re.sub(r"[\s\W_]+", "", t).lower()


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = norm_title(it["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


STRONG = ["cpi", "ppi", "gdp", "通胀", "通缩", "加息", "降息", "利率", "失业", "经济",
          "央行", "逆回购", "进出口", "关税", "汇率", "消费", "工业", "就业", "房价",
          "fed", "inflation", "recession", "oil", "tariff", "growth", "stimulus"]
WEAK = ["市场", "消息", "观点", "分析", "解读", "关注"]


def relevance_score(item):
    text = (item["title"] + " " + item.get("summary", "")).lower()
    return sum(3 for k in STRONG if k in text) + sum(1 for k in WEAK if k in text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    collected, status = [], []

    for i, (name, topic, url) in enumerate(SOURCES):
        try:
            text = fetch_url(url)
            got = parse_rss(text, name, topic)
            status.append({"source": name, "topic": topic, "ok": True, "count": len(got)})
            log(f"[OK] {name:12s} {topic:8s} -> {len(got)} 条", args.verbose)
            collected.extend(got)
        except Exception as e:
            status.append({"source": name, "topic": topic, "ok": False, "error": type(e).__name__})
            log(f"[--] {name:12s} {topic:8s} -> {type(e).__name__}", args.verbose)
        time.sleep(0.25)

    collected = dedupe(collected)
    for it in collected:
        it["score"] = relevance_score(it)
    collected.sort(key=lambda x: x["score"], reverse=True)
    picked = collected[:24]

    result = {
        "fetched_at": datetime.now(CST).isoformat(timespec="seconds"),
        "total_collected": len(collected),
        "sources_status": status,
        "articles": picked,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    ok_sources = sum(1 for s in status if s["ok"] and s.get("count", 0) > 0)
    log(f"\n完成：{ok_sources}/{len(SOURCES)} 个源有数据，去重后 {len(collected)} 条，保留 {len(picked)} 条", True)
    log(f"输出：{OUT}", True)
    if not picked:
        print("警告：本次没有任何源抓到新闻，将依赖上一期简报。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
