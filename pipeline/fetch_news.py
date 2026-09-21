#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日 AI 新闻聚合流水线
Tavily 搜索（近24h，中英文） → Agnes LLM 筛选/摘要/打分 → 去重写入 Supabase
所有凭证从环境变量读取，零硬编码。
"""
import os, sys, json, re, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BEIJING = timezone(timedelta(hours=8))

def env(name, required=True):
    v = os.environ.get(name, "").strip()
    if required and not v:
        print(f"[FATAL] missing env: {name}")
        sys.exit(2)
    return v

def http_json(url, payload=None, headers=None, method=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ---------- 1. Tavily 搜索 ----------
def tavily_search(api_key, query, max_results=15):
    body = {
        "api_key": api_key,
        "query": query,
        "topic": "news",
        "days": 1,                 # 最近 24 小时
        "max_results": max_results,
        "search_depth": "advanced",
    }
    return http_json("https://api.tavily.com/search", body).get("results", [])

def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ai-news-daily/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")

def fetch_tldr_ai(max_items=25):
    """TLDR AI 日报 RSS（海外直连可用）：https://tldr.tech/api/rss/ai"""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(http_get("https://tldr.tech/api/rss/ai"))
        items = []
        for item in root.iter("item"):
            def tag_text(tag):
                el = item.find(tag)
                return (el.text or "").strip() if el is not None else ""
            title, link = tag_text("title"), tag_text("link")
            if not title or not link:
                continue
            items.append({
                "title": title,
                "url": link,
                "content": tag_text("description")[:800],
                "published": tag_text("pubDate"),
            })
        print(f"[rss] TLDR AI -> {len(items)} items")
        return items[:max_items]
    except Exception as e:
        print(f"[rss] TLDR AI failed: {e}")
        return []

def fetch_hf_daily_papers(max_items=15):
    """Hugging Face Daily Papers API：按 upvotes 取当日热门论文"""
    try:
        data = http_json("https://huggingface.co/api/daily_papers", timeout=40)
        rows = []
        for it in data:
            p = it.get("paper") or {}
            pid = p.get("id")
            title = (p.get("title") or "").replace("\n", " ").strip()
            if not pid or not title:
                continue
            rows.append({
                "title": title,
                "url": f"https://huggingface.co/papers/{pid}",
                "content": (p.get("summary") or "").replace("\n", " ").strip()[:800],
                "published": p.get("publishedAt", ""),
                "_upvotes": int(p.get("upvotes") or 0),
            })
        rows.sort(key=lambda r: -r["_upvotes"])
        for r in rows:
            r.pop("_upvotes", None)
        print(f"[api] HF Daily Papers -> {len(rows)} items")
        return rows[:max_items]
    except Exception as e:
        print(f"[api] HF Daily Papers failed: {e}")
        return []

def collect_candidates(tavily_key):
    seen, pool = set(), []
    queries = [
        "artificial intelligence news announcement",
        "AI model release OpenAI Google Anthropic",
        "人工智能 大模型 发布",
        "AI 大模型 最新进展",
        # 定向源：RSS 被反爬/下线的两个源改走 Tavily 站内定向
        "site:venturebeat.com AI",
        "site:jiqizhixin.com 大模型 人工智能",
    ]
    for q in queries:
        try:
            results = tavily_search(tavily_key, q)
            print(f"[tavily] '{q}' -> {len(results)} results")
        except urllib.error.HTTPError as e:
            print(f"[tavily] '{q}' HTTP {e.code}, skip")
            continue
        for r in results:
            url = r.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            pool.append({
                "title": (r.get("title") or "").strip(),
                "url": url,
                "content": (r.get("content") or "").strip()[:800],
                "published": r.get("published_date", ""),
            })
    return pool

# ---------- 2. Agnes LLM 筛选/摘要/打分 ----------
def agnes_chat(base_url, api_key, model, messages, timeout=180):
    return http_json(
        base_url.rstrip("/") + "/chat/completions",
        {"model": model, "messages": messages, "temperature": 0.2},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )

def select_and_summarize(agnes_base, agnes_key, agnes_model, pool, target=10):
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    # 候选过多时压缩送审规模（每条只留必要字段），避免 LLM 长输出转义出错
    MAX_LLM_INPUT = 70
    if len(pool) > MAX_LLM_INPUT:
        pool = pool[:MAX_LLM_INPUT]
        print(f"[llm] pool trimmed to {MAX_LLM_INPUT} for selection")
    items = [{"i": i, "title": c["title"], "url": c["url"],
              "snippet": c["content"][:400], "published": c["published"]}
             for i, c in enumerate(pool)]
    prompt = f"""今天是 {today}（北京时间）。下面是从新闻搜索得到的候选条目（JSON）。
请完成：
1. 挑出最重要的 {target} 条左右（8-12条均可）关于 AI 技术的新闻，剔除营销软文、重复事件、与AI无关的内容；
2. 每条写 60-100 字的中文摘要（概括核心技术点）；
3. 按重要性打分 1-10（10 = 全行业重大，如顶级实验室新模型/重大政策；5-6 = 常规产品更新；1-2 = 边缘资讯）；
4. lang 字段：原始新闻是中文标 zh，英文标 en；
5. title 字段：给出简洁的中文标题（15字内）。

只输出 JSON 数组，格式：
[{{"idx": <候选编号i>, "title": "<中文标题>", "summary_zh": "<中文摘要>", "score": <1-10>, "lang": "zh|en"}}]

候选条目：
{json.dumps(items, ensure_ascii=False)}"""
    last_err = None
    for attempt in (1, 2):
        resp = agnes_chat(agnes_base, agnes_key, agnes_model,
                          [{"role": "user", "content": prompt}])
        text = resp["choices"][0]["message"]["content"]
        # 剥掉可能的 ```json 围栏
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            last_err = ValueError(f"LLM output has no JSON array: {text[:300]}")
            continue
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            last_err = e
            # 重试时强调转义要求
            prompt += "\n\n注意：上一次输出 JSON 解析失败。所有字符串内的双引号必须写成 \\\"，只输出合法 JSON 数组，不要任何多余文本。"
    raise last_err

# ---------- 3. Supabase 写入（service_role 绕过 RLS） ----------
def supabase_upsert(url, service_key, rows):
    endpoint = url.rstrip("/") + "/rest/v1/news_items"
    req = urllib.request.Request(endpoint + "?on_conflict=url",
                                 data=json.dumps(rows).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("apikey", service_key)
    req.add_header("Authorization", f"Bearer {service_key}")
    req.add_header("Prefer", "resolution=merge-duplicates,return=representation")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

# ---------- main ----------
def main():
    dry_run = "--dry-run" in sys.argv
    tavily_key = env("TAVILY_API_KEY")
    agnes_key  = env("AGNES_API_KEY")
    agnes_base = env("AGNES_BASE_URL", required=False) or "https://api.agnes-ai.cn/v1"
    agnes_model = env("AGNES_MODEL", required=False) or "agnes-2.5-flash"
    sb_url  = env("SUPABASE_URL", required=not dry_run)
    sb_key  = env("SUPABASE_SERVICE_ROLE_KEY", required=not dry_run)

    print(f"[run] {datetime.now(BEIJING).strftime('%Y-%m-%d %H:%M')} Beijing")

    # 新增源先行：TLDR RSS + HF Daily Papers（与 Tavily 候选按 URL 去重合并）
    pool = []
    seen = set()
    for src in (fetch_tldr_ai(), fetch_hf_daily_papers()):
        for it in src:
            if it["url"] and it["url"] not in seen:
                seen.add(it["url"])
                pool.append(it)
    print(f"[sources] TLDR+HF -> {len(pool)} items after dedup")

    for it in collect_candidates(tavily_key):
        if it["url"] not in seen:
            seen.add(it["url"])
            pool.append(it)
    print(f"[pool] {len(pool)} unique candidates")
    if not pool:
        print("[warn] no candidates from Tavily; exit 0 (nothing to do)")
        return

    picks = select_and_summarize(agnes_base, agnes_key, agnes_model, pool)
    print(f"[llm] {len(picks)} picked")

    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    rows, seen_urls = [], set()
    for p in picks:
        try:
            c = pool[p["idx"]]
        except (KeyError, IndexError):
            continue
        if c["url"] in seen_urls:
            continue
        seen_urls.add(c["url"])
        rows.append({
            "published_date": today,
            "title": (p.get("title") or c["title"])[:120],
            "title_en": c["title"][:200] if p.get("lang") == "en" else None,
            "url": c["url"],
            "summary_zh": p.get("summary_zh", "")[:500],
            "score": max(1, min(10, int(p.get("score", 5)))),
            "source": urllib.parse.urlparse(c["url"]).netloc,
            "lang": p.get("lang") if p.get("lang") in ("zh", "en") else "en",
        })
    rows.sort(key=lambda r: -r["score"])

    if dry_run:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        print(f"[dry-run] would upsert {len(rows)} rows")
        return

    inserted = supabase_upsert(sb_url, sb_key, rows)
    print(f"[supabase] upserted {len(inserted)} rows")
    for r in inserted:
        print(f"  [{r['score']:>2}] {r['title']}")

if __name__ == "__main__":
    main()
