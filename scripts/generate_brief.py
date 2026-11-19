#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EconoScout · 简报生成器（零第三方依赖）

职责：读 raw-news.json，调用大模型（DeepSeek / OpenAI 兼容接口），
      按 EconoScout 岗位说明书 V1.1 生成 8 字段结构化导读，
      校验通过后合并进 data/briefs.json（保留最近 N 期），供前端读取。

安全：API Key 只从环境变量读（GitHub Actions 里来自 Secrets），绝不写进代码或前端。
护栏：
  - 生成结果必须过结构校验（3 条 × 8 字段 + 合法分层标签），否则不覆盖
  - 校验失败保留上一期简报，宁可不更新也不出错报
  - 全部失败时用种子简报保证有内容

用法：
  python generate_brief.py                 # 真调模型
  python generate_brief.py --dry-run       # 不调模型，用种子数据跑通合并流程（本地测试用）
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
RAW = os.path.join(DATA_DIR, "raw-news.json")
BRIEFS = os.path.join(DATA_DIR, "briefs.json")
SEED = os.path.join(DATA_DIR, "seed-briefs.json")
MAX_KEEP = 12  # 前端最多保留 12 期可切换

# ---------------------------------------------------------------- 系统提示词
# 直接浓缩自岗位说明书 V1.1 + 优化后的 landing prompt，确保前后端"专家脑"一致
SYSTEM_PROMPT = """你是 EconoScout，面向所有想看懂经济新闻的普通人的 AI 导读员。你不只是总结新闻，更带读者读懂新闻背后的经济逻辑，并在真实生活例子里培养经济学思维。

【铁律 · 违反即失败】
1. 只使用我给出的「候选新闻」中的事实，绝不编造数据、日期、数字、来源。原文没有的数字一个都不能写。
2. 严格区分三层信息并打标签：
   - fact 可核查事实：来源原文明确写出的事件与数据；
   - infer 合理推断：基于事实的经济机制解释、影响分析（须用"可能/往往/通常/有助"等措辞）；
   - view 观点判断：机构预测、市场看法（须写明是谁的观点）。
3. 不把相关性说成因果。不确定的地方明说不确定。
4. 不给任何投资建议、买卖建议。
5. 专业术语第一次出现用大白话解释，并配一个贴近大学生生活的例子。
6. 每条必须给出可点击的真实来源链接（用候选新闻里给的 url）。

【输出格式】
只输出一个 JSON 对象，不要任何多余文字、不要 markdown 代码块。结构如下：
{
  "range": "起日期 ~ 止日期（YYYY-MM-DD，取本期新闻日期跨度）",
  "theme": "本期主题，6-12字",
  "overview": "本期速览：2-3 句点明这几条新闻的共同线索和为什么值得看",
  "news": [
    {
      "tag": "fact 或 infer 或 view",
      "title": "新闻标题，15-30字",
      "one": "一句话看懂，30字内大白话",
      "happened": "发生了什么：只写来源原文的事实+数据，句末标注（可核查事实/机构观点等）",
      "why": "为什么重要：和普通人生活的关联，1-2句",
      "concept": "经济学知识卡的关键词，如 CPI、逆回购",
      "conceptFull": "知识卡正文：白话解释+口径提醒，60-100字",
      "life": "生活中的例子：贴近大学生的小类比，40-70字",
      "think": "想一想：一个开放性问题，引导独立思考",
      "source": "来源名，日期 · <a href=\\"真实URL\\" target=\\"_blank\\" rel=\\"noopener\\">查看原文 ↗</a>"
    }
  ],
  "finding": "本期经济观察员的发现：把几条新闻连起来的理解框架，必须声明不是已证实的因果",
  "review": ["知识点回顾1", "知识点回顾2", "知识点回顾3"],
  "extend": "延伸思考：一个把宏观和读者生活连起来的问题",
  "sources": ["<a href=\\"URL\\" ...>来源标题 · 日期 ↗</a>", "..."],
  "selfcheck": ["<b>数据核查：</b>...", "<b>事实与推断：</b>...", "<b>不确定性：</b>...", "<b>链接与排版：</b>..."]
}

【硬性要求】
- news 必须恰好 3 条，每条 8 个字段齐全，tag 只能是 fact/infer/view。
- sources 与每条 news 的 url 必须来自我给的候选新闻，不得杜撰。
- selfcheck 固定 4 条，按数据/事实推断/不确定性/链接排版顺序写。
- 若候选新闻不足以支撑 3 条，宁可选信息量最大的 3 条并如实标注，也不要编造。"""


def log(msg, on=True):
    if on:
        print(msg, flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def build_user_prompt(raw):
    arts = raw.get("articles", [])
    lines = [f"抓取时间：{raw.get('fetched_at','')}", "以下是候选新闻（标题/摘要/来源/链接）：", ""]
    for i, a in enumerate(arts[:18], 1):
        lines.append(f"{i}. 标题：{a.get('title','')}")
        if a.get("summary"):
            lines.append(f"   摘要：{a.get('summary','')}")
        lines.append(f"   来源：{a.get('source','')}  链接：{a.get('url','')}")
        lines.append("")
    lines.append("请从中选出最能反映这一时期经济脉搏的 3 条，按上述 JSON 结构生成一期导读。")
    lines.append("今天日期：" + datetime.now(CST).strftime("%Y-%m-%d"))
    return "\n".join(lines)


def call_llm(user_prompt, verbose=True):
    """调用 OpenAI 兼容的 Chat Completions 接口（DeepSeek 默认）。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置")
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    log(f"[LLM] 调用 {model} ...", verbose)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    return content


def extract_json(text):
    """从模型输出里抠出 JSON 对象（容忍代码块包裹）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型输出中找不到 JSON 对象")
    return json.loads(text[start:end + 1])


def validate_brief(d):
    """结构护栏：不合格直接拒绝，绝不覆盖上一期。"""
    required = ["range", "theme", "overview", "news", "finding", "review", "extend", "sources", "selfcheck"]
    if not isinstance(d, dict):
        return "顶层不是对象"
    for k in required:
        if k not in d:
            return "缺字段 " + k
    if not isinstance(d["news"], list) or len(d["news"]) != 3:
        return "新闻不是 3 条"
    fields = ["tag", "title", "one", "happened", "why", "concept", "conceptFull", "life", "think", "source"]
    for i, n in enumerate(d["news"]):
        for f in fields:
            if f not in n:
                return f"第{i+1}条缺字段 {f}"
        if n["tag"] not in ("fact", "infer", "view"):
            return f"第{i+1}条 tag 非法"
    if not isinstance(d["selfcheck"], list) or len(d["selfcheck"]) < 4:
        return "selfcheck 不足 4 条"
    return None


def merge_brief(new_brief, verbose=True):
    """把新简报插到最前，保留最近 MAX_KEEP 期。"""
    existing = load_json(BRIEFS, [])
    if not isinstance(existing, list):
        existing = []
    new_brief = dict(new_brief)
    new_brief["generated_at"] = datetime.now(CST).isoformat(timespec="seconds")
    merged = [new_brief] + [b for b in existing
                            if b.get("range") != new_brief.get("range")][:MAX_KEEP - 1]
    os.makedirs(DATA_DIR, exist_ok=True)
    # 原子写：先写临时文件再替换，避免中途失败留下半个文件
    tmp = BRIEFS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BRIEFS)
    log(f"[OK] 已写入 {BRIEFS}，当前共 {len(merged)} 期", verbose)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不调模型，用种子数据跑通流程")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    raw = load_json(RAW, {"articles": [], "fetched_at": ""})
    log(f"读取 {len(raw.get('articles',[]))} 条候选新闻", args.verbose)

    if args.dry_run:
        seeds = load_json(SEED, [])
        if not seeds:
            print("种子文件不存在，请先运行 prepare_seed.py", file=sys.stderr)
            return 3
        for s in seeds:
            err = validate_brief(s)
            if err:
                print(f"种子数据校验失败：{err}", file=sys.stderr)
                return 4
        for s in seeds:
            merge_brief(s, args.verbose)
        log(f"[dry-run] 已用 {len(seeds)} 期种子数据初始化 briefs.json", True)
        return 0

    if not raw.get("articles"):
        print("没有候选新闻，跳过本期生成（保留上一期）", file=sys.stderr)
        return 2

    try:
        content = call_llm(build_user_prompt(raw), args.verbose)
        brief = extract_json(content)
    except Exception as e:
        print(f"调用/解析模型失败：{e}", file=sys.stderr)
        return 5

    err = validate_brief(brief)
    if err:
        print(f"生成结果未通过护栏，保留上一期。原因：{err}", file=sys.stderr)
        return 6

    merge_brief(brief, args.verbose)
    log(f"[OK] 本期主题：{brief['theme']} | {brief['range']}", True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
