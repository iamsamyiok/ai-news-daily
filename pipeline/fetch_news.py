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

def collect_candidates(tavily_key):
    seen, pool = set(), []
    queries = [
        "artificial intelligence news announcement",
        "AI model release OpenAI Google Anthropic",
        "人工智能 大模型 发布",
        "AI 大模型 最新进展",
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
    resp = agnes_chat(agnes_base, agnes_key, agnes_model,
                      [{"role": "user", "content": prompt}])
    text = resp["choices"][0]["message"]["content"]
    # 剥掉可能的 ```json 围栏
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError(f"LLM output has no JSON array: {text[:300]}")
    return json.loads(m.group(0))

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

    pool = collect_candidates(tavily_key)
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
