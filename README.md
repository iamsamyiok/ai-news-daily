# AI 日报 · 每日 AI 新闻聚合站

每天早 8 点（北京时间）自动抓取最近 24 小时的中英文 AI 新闻，LLM 筛选 10 条最重要的、写中文摘要、打重要性分，展示在单页站点上。

**全免费**：Vercel Hobby + Supabase Free + GitHub Actions + Tavily Free + Agnes LLM。

## 架构

```
GitHub Actions (cron 08:00 北京时间)
  └─ pipeline/fetch_news.py (纯 stdlib，零依赖)
       ├─ Tavily API   → 搜近 24h 新闻（中英文 4 组查询）
       ├─ Agnes LLM    → 筛选/中文摘要/重要性打分
       └─ Supabase REST → 去重 upsert 写入（service_role，绕过 RLS）
                              ↓ RLS 公开读
       web/index.html (Tailwind CDN 单页) ← Vercel 托管
```

## 部署步骤

1. **建表**：Supabase Dashboard → SQL Editor → 执行 `supabase/schema.sql`（含 RLS 公开读）
2. **推代码**：推到 GitHub 仓库
3. **配 Secrets**（Settings → Secrets and variables → Actions）：
   | Secret | 说明 |
   |---|---|
   | `TAVILY_API_KEY` | tavily.com 免费层 |
   | `AGNES_API_KEY` / `AGNES_BASE_URL` / `AGNES_MODEL` | LLM |
   | `SUPABASE_URL` | 项目设置 → API → Project URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | 项目设置 → API → service_role（**绝不外泄**） |
4. **部署前端**：`cd web && vercel deploy --prod`
5. **填 config.js**：把 `SUPABASE_URL` 和 `SUPABASE_ANON_KEY`（anon 公开 key，受 RLS 保护）填入 `web/config.js` 后重新部署
6. 手动触发一次 workflow（Actions → daily-ai-news → Run workflow）验证

## 本地测试流水线

```bash
export TAVILY_API_KEY=... AGNES_API_KEY=... SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=...
python pipeline/fetch_news.py --dry-run   # 不写库，打印结果
python pipeline/fetch_news.py             # 正式写入
```
