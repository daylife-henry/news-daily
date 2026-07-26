#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 版每日新闻工作流
================================
在 GitHub Actions 中运行，无需本地电脑开机。

功能：
  1. 从 uapis.cn 抓取抖音热榜（前5最新最热+后5热门作品）+ 头条热榜（按热度排序）
  2. 从 GitHub Search API 抓取近15天热门项目（渐进式扩展确保10条）+ 年度热门15个
  3. 与历史记录比对去重（30天）+ 跨来源去重
  4. 获取 GitHub 项目 README 摘要（前2000字）
  5. 可选：用 DeepSeek AI 对每条新闻做 ≤50字 归纳总结
  6. 生成 Markdown 日报（含更新时间、README摘要）
  7. 通过 PushPlus 推送到微信
  8. 更新历史记录文件（由 GitHub Actions 自动提交回仓库）

环境变量（在 GitHub Secrets 中配置）：
  PUSHPLUS_TOKEN    - PushPlus 推送 token（必需）
  DEEPSEEK_API_KEY  - DeepSeek API 密钥（可选，没有则直接使用标题/description）
"""

import json
import os
import sys
import re
import time
import glob as glob_module
import urllib.request
import urllib.error
from datetime import datetime, timedelta

# ============================================================
# 配置
# ============================================================

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()

API_BASE = "https://uapis.cn/api/v1/misc/hotboard"
NEWS_COUNT_PER_SOURCE = 10
DEDUP_LOOKBACK_DAYS = 30

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

SOURCES = {
    "douyin": {
        "name": "抖音热点榜",
        "emoji": "🔥",
        "type": "douyin",
        "sort_by": "hot_value"
    },
    "toutiao": {
        "name": "今日头条热榜",
        "emoji": "📰",
        "type": "toutiao",
        "sort_by": "hot_value"
    },
    "github": {
        "name": "GitHub 热门项目",
        "emoji": "⭐",
        "type": "github",
        "sort_by": "stars",
        "days": 15,
        "count": 10
    },
    "github_yearly": {
        "name": "GitHub 年度热门",
        "emoji": "🏆",
        "type": "github_yearly",
        "sort_by": "stars",
        "count": 15
    }
}

# ============================================================
# 工具函数
# ============================================================

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url, payload, timeout=30, headers=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason} | body: {body}") from e


def normalize_title(title):
    t = title.strip()
    t = re.sub(r'[\s\u3000\u00a0]+', '', t)
    t = re.sub(r'[]，。！？、；：\u201c\u201d\u2018\u2019（）【】《》,.!?:;"\'()[<>]', '', t)
    return t.lower()


def titles_similar(t1, t2):
    n1 = normalize_title(t1)
    n2 = normalize_title(t2)
    if n1 == n2:
        return True
    if len(n1) > 10 and len(n2) > 10:
        shorter = n1 if len(n1) < len(n2) else n2
        longer = n2 if len(n1) < len(n2) else n1
        if shorter in longer:
            return True
        if n1[:15] == n2[:15]:
            return True
    return False


def format_hot_value(hot_num):
    if not hot_num or hot_num == 0:
        return ""
    if hot_num >= 10000:
        return f"{hot_num / 10000:.1f}万"
    return str(hot_num)


def format_github_date(iso_str):
    """将 GitHub ISO 8601 时间格式化为 YYYY-MM-DD"""
    if not iso_str:
        return ""
    try:
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso_str[:10] if len(iso_str) >= 10 else iso_str


def is_metadata_line(line):
    """
    2026-07-26 | Henry | 判断是否为元数据噪音行
    过滤掉 Language 列表、License、徽章、纯 URL、表格、统计行、多语言导航等非内容行
    """
    line = line.strip()
    if not line:
        return True
    # 纯 URL 行
    if re.match(r'^(https?://|www\.)', line, re.IGNORECASE):
        return True
    # 警告/注意类徽章前缀 [!WARNING] [!NOTE] 等
    if re.match(r'^\[!(WARNING|NOTE|TIP|IMPORTANT|CAUTION)\]', line, re.IGNORECASE):
        return True
    # 已被剥掉 > 的引用块内容
    if re.match(r'^(Official\s+sources|Note|Warning|Tip|Important|Caution|Disclaimer)\b', line, re.IGNORECASE):
        return True
    # "Read this in other languages" / "Available in" / "Translations" 等导航头
    if re.search(r'\b(Read this in other languages|Available in|Translations?|Choose your language|Switch language)\b', line, re.IGNORECASE):
        return True
    # 多语言导航单行
    if re.search(r'[/|]', line):
        tokens = [t.strip() for t in re.split(r'[/|]', line) if t.strip()]
        if len(tokens) >= 3:
            short_tokens = [t for t in tokens if len(t) <= 12]
            if len(short_tokens) / len(tokens) >= 0.6:
                return True
    # 多语言导航单元素行（多行格式的每行）
    if re.match(r'^[\s\|]*(English|Português|简体中文|繁體中文|繁體|日本語|한국어|Türkçe|Русский|Tiếng\s*Việt|ไทย|Deutsch|Español|Français|Italiano|العربية|हिन्दी|Polski|Nederlands|Svenska|Українська)\s*\|?\s*$', line, re.IGNORECASE):
        return True
    if re.match(r'^\|\s*(English|Português|简体中文|繁體中文|繁體|日本語|한국어|Türkçe|Русский|Tiếng\s*Việt|ไทย|Deutsch|Español|Français|Italiano|العربية|हिन्दी|Polski|Nederlands|Svenska|Українська)\s*$', line, re.IGNORECASE):
        return True
    # License 信息行
    if re.match(r'^License\s*[:|]', line, re.IGNORECASE):
        return True
    if re.search(r'\b(MIT|Apache|GPL|BSD|MPL|LGPL|ISC|Mozilla|Unlicense)\s+License\b', line, re.IGNORECASE):
        return True
    # Sponsor / Donate / Funding / 独家赞助
    if re.match(r'^(Sponsor|Sponsor this|Donate|Funding|Backers?|Sponsors?|独家赞助)\b', line, re.IGNORECASE):
        return True
    # 导航行: 用 · 分隔的短段链接
    if line.count('·') >= 2:
        tokens = [t.strip() for t in line.split('·')]
        if tokens and all(len(t) < 25 for t in tokens) and not re.search(r'[。.!]', line):
            return True
    # GitHub 统计行
    if line.count('|') >= 2 and re.search(r'\b\d+[KkMmBb]?\+?\s*(stars?|forks?|contributors?|watchers?|issues?|pull\s*requests?|PRs?|downloads?|users?|dependents?|releases?|language\s+ecosystems?)\b', line, re.IGNORECASE):
        return True
    # 表格行（多 pipe 且所有段都短）
    if line.count('|') >= 2:
        tokens = [t.strip() for t in line.split('|')]
        if tokens and all(len(t) < 30 for t in tokens):
            return True
    # 表格分隔行
    if re.match(r'^[\s\-:|]+$', line) and '-' in line:
        return True
    # 太短
    if len(line) < 3:
        return True
    return False


def clean_readme(text, max_chars=4000):
    """
    2026-07-26 | Henry | 清理 README markdown 为纯文本摘要
    去除 HTML 标签、图片、链接、代码块等格式，保留核心文字内容
    """
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\[!\[.*?\]\(.*?\)\]\(.*?\)', '', text)
    text = re.sub(r'\[\s*\]\([^)]+\)', '', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\-*=]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\s]*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_]+)_{1,3}', r'\1', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&nbsp;', ' ')
    text = re.sub(r'\n{3,}', '\n\n', text)
    raw_lines = text.split('\n')
    cleaned_lines = [line.strip() for line in raw_lines if not is_metadata_line(line)]
    final_lines = []
    prev_blank = False
    for line in cleaned_lines:
        is_blank = (line == '')
        if is_blank and prev_blank:
            continue
        final_lines.append(line)
        prev_blank = is_blank
    text = '\n'.join(final_lines).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + '...'
    return text


def fetch_github_readme(repo):
    """通过 raw.githubusercontent.com 获取项目 README 摘要"""
    full_name = repo.get("title", "")
    branch = repo.get("default_branch", "main")
    if "/" not in full_name:
        return ""
    owner, repo_name = full_name.split("/", 1)
    readme_filenames = ["README.md", "readme.md", "README.MD", "README.rst", "README.txt", "README"]
    for filename in readme_filenames:
        url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{filename}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "news-cloud-workflow"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                if content:
                    return clean_readme(content, max_chars=4000)
        except Exception:
            continue
    return ""


# ============================================================
# 历史记录
# ============================================================

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"records": [], "sent_titles": []}
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_history(history):
    cutoff = (datetime.now() - timedelta(days=DEDUP_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    history["records"] = [r for r in history.get("records", []) if r.get("date", "") >= cutoff]
    history["sent_titles"] = [r.get("normalized_title", "") for r in history["records"]]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    log(f"历史记录已保存，共 {len(history['records'])} 条")


def is_duplicate(title, history):
    normalized = normalize_title(title)
    for sent_title in history.get("sent_titles", []):
        if not sent_title:
            continue
        if normalized == sent_title:
            return True
        if len(normalized) > 8 and len(sent_title) > 8:
            if normalized[:15] == sent_title[:15]:
                return True
            if normalized in sent_title or sent_title in normalized:
                return True
    return False


# ============================================================
# 数据抓取
# ============================================================

def fetch_hotlist(source_type, sort_by="hot_value"):
    url = f"{API_BASE}?type={source_type}"
    log(f"正在抓取 {source_type} 热榜: {url}")
    try:
        data = http_get(url, timeout=20)
    except Exception as e:
        log(f"抓取 {source_type} 失败: {e}")
        time.sleep(3)
        try:
            data = http_get(url, timeout=30)
        except Exception as e2:
            log(f"重试抓取 {source_type} 仍然失败: {e2}")
            return []

    raw_list = data.get("list", [])
    if not raw_list:
        log(f"{source_type} 热榜返回空数据")
        return []

    news_list = []
    for item in raw_list:
        title = item.get("title", "").strip()
        if not title:
            continue
        hot_value = item.get("hot_value", "")
        extra = item.get("extra", {})

        hot_num = 0
        if isinstance(hot_value, (int, float)):
            hot_num = hot_value
        elif isinstance(hot_value, str) and hot_value.isdigit():
            hot_num = int(hot_value)
        elif extra.get("hot_value"):
            hot_num = extra.get("hot_value", 0)

        news = {
            "title": title,
            "url": item.get("url", ""),
            "hot_value": hot_num,
            "view_count": extra.get("view_count", 0),
            "video_count": extra.get("video_count", 0),
            "index": item.get("index", 0),
            "source": source_type,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        news_list.append(news)

    if sort_by == "hot_value" and any(n["hot_value"] for n in news_list):
        news_list.sort(key=lambda x: x["hot_value"], reverse=True)
    else:
        news_list.sort(key=lambda x: x.get("index", 999))

    log(f"成功抓取 {source_type} 热榜 {len(news_list)} 条")
    return news_list


def fetch_github_trending(days=15, count=10):
    """从 GitHub Search API 抓取最近 N 天内 star 最多的项目"""
    since_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (
        f"https://api.github.com/search/repositories"
        f"?q=created:>{since_date}&sort=stars&order=desc&per_page={count}"
    )

    log(f"正在抓取 GitHub 热门项目（近{days}天）: {url}")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "news-cloud-workflow",
            "Accept": "application/vnd.github+json"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"抓取 GitHub 热门项目失败: {e}")
        time.sleep(3)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "news-cloud-workflow",
                "Accept": "application/vnd.github+json"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e2:
            log(f"重试抓取 GitHub 热门项目仍然失败: {e2}")
            return []

    items = data.get("items", [])
    if not items:
        log("GitHub 热门项目返回空数据")
        return []

    repos = []
    for item in items:
        repo = {
            "title": item.get("full_name", ""),
            "url": item.get("html_url", ""),
            "description": item.get("description") or "",
            "language": item.get("language") or "",
            "stars": item.get("stargazers_count", 0),
            "source": "github",
            "pushed_at": item.get("pushed_at", ""),
            "created_at": item.get("created_at", ""),
            "default_branch": item.get("default_branch", "main"),
            "topics": item.get("topics", [])
        }
        repos.append(repo)

    log(f"成功抓取 GitHub 热门项目 {len(repos)} 个（近{days}天）")
    return repos


def fetch_github_yearly_top(count=15):
    """抓取当前年份 star 最多的项目，不做历史去重"""
    year = datetime.now().year
    url = (
        f"https://api.github.com/search/repositories"
        f"?q=created:>{year}-01-01&sort=stars&order=desc&per_page={min(count * 2, 30)}"
    )

    log(f"正在抓取 GitHub 年度热门项目（{year}年）: {url}")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "news-cloud-workflow",
            "Accept": "application/vnd.github+json"
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log(f"抓取 GitHub 年度热门失败: {e}")
        time.sleep(3)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "news-cloud-workflow",
                "Accept": "application/vnd.github+json"
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e2:
            log(f"重试抓取 GitHub 年度热门仍然失败: {e2}")
            return []

    items = data.get("items", [])
    if not items:
        log("GitHub 年度热门返回空数据")
        return []

    repos = []
    for item in items[:count]:
        repo = {
            "title": item.get("full_name", ""),
            "url": item.get("html_url", ""),
            "description": item.get("description") or "",
            "language": item.get("language") or "",
            "stars": item.get("stargazers_count", 0),
            "source": "github_yearly",
            "pushed_at": item.get("pushed_at", ""),
            "created_at": item.get("created_at", ""),
            "default_branch": item.get("default_branch", "main"),
            "topics": item.get("topics", [])
        }
        repos.append(repo)

    log(f"成功抓取 GitHub 年度热门项目 {len(repos)} 个（{year}年）")
    return repos


# ============================================================
# AI 归纳总结（DeepSeek）
# ============================================================

def _deepseek_chat(prompt, max_tokens=2000, timeout=60, batch_label=""):
    """调用 DeepSeek API，返回 content 字符串；失败返回 None 并记录详细错误"""
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens
    }
    try:
        result = http_post_json(
            "https://api.deepseek.com/chat/completions",
            payload,
            timeout=timeout,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
        )
        if "error" in result:
            log(f"DeepSeek API 返回错误{batch_label}: {result['error']}")
            return None
        content = result["choices"][0]["message"]["content"].strip()
        if not content:
            log(f"DeepSeek API 返回空内容{batch_label}")
            return None
        return content
    except Exception as e:
        log(f"DeepSeek API 调用异常{batch_label}: {type(e).__name__}: {e}")
        return None


def _github_fallback(repo):
    """API 失败时的本地兜底：优先 README 首句，其次 description"""
    readme_excerpt = repo.get("readme_excerpt", "")
    desc = repo.get("description", "")
    summary = _first_meaningful_line(readme_excerpt, max_len=40)
    if not summary:
        summary = (desc or repo["title"]).strip()
        if len(summary) > 40:
            summary = summary[:40]
    return summary


def ai_summarize(news_list):
    """用 DeepSeek API 对新闻列表做 ≤50字 归纳总结"""
    if not DEEPSEEK_API_KEY:
        log("未配置 DEEPSEEK_API_KEY，直接使用标题作为总结")
        return {n["title"]: n["title"] for n in news_list}

    log(f"正在用 DeepSeek AI 对 {len(news_list)} 条新闻做归纳总结...")

    news_lines = []
    for i, news in enumerate(news_list, 1):
        news_lines.append(f"{i}. {news['title']}")

    prompt = f"""请对以下{len(news_list)}条新闻标题逐一做归纳总结，每条总结不超过50个中文字。
要求：准确概括核心内容，简洁有力，不要添加标题中没有的信息。

新闻列表：
{chr(10).join(news_lines)}

请严格按以下JSON格式返回，不要包含任何其他文字：
{{"summaries": ["总结1", "总结2", ...]}}"""

    try:
        content = _deepseek_chat(prompt, max_tokens=2000, timeout=60, batch_label="（新闻总结）")
        if content is None:
            log("AI 总结失败，将使用原标题")
            return {n["title"]: n["title"] for n in news_list}

        if content.startswith("```"):
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)

        parsed = json.loads(content)
        summaries = parsed.get("summaries", [])

        if len(summaries) != len(news_list):
            log(f"警告: AI 返回 {len(summaries)} 条总结，期望 {len(news_list)} 条，将按顺序匹配")

        result_map = {}
        for i, news in enumerate(news_list):
            if i < len(summaries):
                summary = summaries[i].strip()
                if len(summary) > 50:
                    summary = summary[:50]
                result_map[news["title"]] = summary
            else:
                result_map[news["title"]] = news["title"]

        log(f"AI 总结完成，共 {len(result_map)} 条")
        return result_map

    except Exception as e:
        log(f"AI 总结失败: {e}，将使用原标题")
        return {n["title"]: n["title"] for n in news_list}


def _first_meaningful_line(text, max_len=40):
    """从 README 摘要中提取第一句有意义的中文描述（≤max_len字）"""
    if not text:
        return ""
    # 分行，跳过元数据噪音行
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 跳过常见的元数据/徽章起始
        if re.match(r'^[\s\-=*#|>]+', line):
            continue
        if re.match(r'^(Language|License|Build|Made|Powered|Created|Written)\b', line, re.IGNORECASE):
            continue
        if line.count('|') >= 2:
            continue
        # 跳过纯符号/URL
        if re.match(r'^(https?://|www\.)', line, re.IGNORECASE):
            continue
        if len(line) < 5:
            continue
        # 截到第一个句号/感叹号/问号
        m = re.search(r'[。！？!?\.]', line)
        if m:
            sentence = line[:m.start() + 1].strip()
        else:
            sentence = line
        if len(sentence) > max_len:
            sentence = sentence[:max_len].rstrip("，,;；") + "。"
        return sentence
    return ""


def ai_explain_github(repos):
    """用 DeepSeek API 对 GitHub 项目做中文一句话高度概括（≤40字），分批调用避免超时"""
    if not repos:
        return {}

    if not DEEPSEEK_API_KEY:
        log("未配置 DEEPSEEK_API_KEY，GitHub 项目使用 README/description 本地兜底")
        return {repo["title"]: _github_fallback(repo) for repo in repos}

    log(f"正在用 DeepSeek AI 对 {len(repos)} 个 GitHub 项目做中文一句话概括...")

    BATCH_SIZE = 10
    result_map = {}

    for batch_idx in range(0, len(repos), BATCH_SIZE):
        batch = repos[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1
        total_batches = (len(repos) + BATCH_SIZE - 1) // BATCH_SIZE
        log(f"  处理第 {batch_num}/{total_batches} 批（{len(batch)} 个项目）...")

        repo_lines = []
        for i, repo in enumerate(batch, 1):
            name = repo["title"]
            lang = repo.get("language", "")
            lang_str = f" [{lang}]" if lang else ""
            readme_preview = ""
            if repo.get("readme_excerpt"):
                readme_preview = repo["readme_excerpt"][:400]
            desc = repo.get("description") or ""
            info = f"描述: {desc}"
            if readme_preview:
                info += f"\n  README摘要: {readme_preview}"
            repo_lines.append(f"{i}. {name}{lang_str}: {info}")

        prompt = f"""请对以下{len(batch)}个GitHub开源项目逐一用中文做一句话高度概括，每个概括**严格控制在一句话以内，不超过40个中文字**。

要求：
- 必须是一句完整的中文话（以"。"或自然断句结尾），简洁说明项目是做什么的
- 让不熟悉该项目的开发者也能一眼看懂核心功能
- 优先参考README摘要中的核心描述
- 不要包含 Language 徽章列表、License 信息、URL、徽章文本、表格内容
- 不要写"这是一个..."、"该项目..."这种套话开头
- 不要超过一句话

项目列表：
{chr(10).join(repo_lines)}

请严格按以下JSON格式返回，不要包含任何其他文字：
{{"summaries": ["说明1", "说明2", ...]}}"""

        content = _deepseek_chat(prompt, max_tokens=1500, timeout=60,
                                 batch_label=f"（第{batch_num}批）")
        if content is None:
            log(f"  第 {batch_num} 批 AI 调用失败，使用本地兜底")
            for repo in batch:
                result_map[repo["title"]] = _github_fallback(repo)
            continue

        # 清理 markdown 代码块包裹
        if content.startswith("```"):
            content = re.sub(r'^```(?:json)?\s*', '', content)
            content = re.sub(r'\s*```$', '', content)

        try:
            parsed = json.loads(content)
            summaries = parsed.get("summaries", [])
        except json.JSONDecodeError as e:
            log(f"  第 {batch_num} 批 JSON 解析失败: {e}")
            log(f"  原始返回前200字: {content[:200]}")
            for repo in batch:
                result_map[repo["title"]] = _github_fallback(repo)
            continue

        if len(summaries) != len(batch):
            log(f"  警告: 第 {batch_num} 批 AI 返回 {len(summaries)} 条，期望 {len(batch)} 条，按顺序匹配")

        for i, repo in enumerate(batch):
            if i < len(summaries):
                summary = summaries[i].strip()
                m = re.search(r'[。！？!?]', summary)
                if m and m.start() < len(summary) - 1:
                    summary = summary[:m.start() + 1]
                if len(summary) > 40:
                    summary = summary[:40].rstrip("，,;；") + "。"
                result_map[repo["title"]] = summary
            else:
                result_map[repo["title"]] = _github_fallback(repo)

        log(f"  第 {batch_num} 批完成，已获取 {min(len(summaries), len(batch))} 条说明")
        if batch_idx + BATCH_SIZE < len(repos):
            time.sleep(1)

    log(f"GitHub 项目说明完成，共 {len(result_map)} 条")
    return result_map


# ============================================================
# 生成 Markdown
# ============================================================

def generate_markdown(all_news, summaries):
    today = datetime.now().strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    weekday = weekdays[datetime.now().weekday()]

    douyin_count = sum(1 for n in all_news if n["source"] == "douyin")
    toutiao_count = sum(1 for n in all_news if n["source"] == "toutiao")
    github_count = sum(1 for n in all_news if n["source"] == "github")
    github_yearly_count = sum(1 for n in all_news if n["source"] == "github_yearly")

    md = f"# 📰 每日新闻日报\n\n"
    md += f"> {today} 周{weekday} | 抖音{douyin_count}条 + 头条{toutiao_count}条 + GitHub近15天{github_count}�� + 年度热门{github_yearly_count}个\n\n"
    md += "---\n\n"

    # 抖音部分
    douyin_news = [n for n in all_news if n["source"] == "douyin"]
    douyin_hot = [n for n in douyin_news if n.get("douyin_type") == "最新最热"]
    douyin_views = [n for n in douyin_news if n.get("douyin_type") == "热门作品"]

    if douyin_news:
        md += "## 🔥 抖音热点榜\n\n"
        md += "**▸ 最新最热（按综合热度排序）**\n\n"
        for i, news in enumerate(douyin_hot, 1):
            hot_str = f" `🔥{news.get('hot_display', '')}`" if news.get("hot_display") else ""
            updated = f" `🕒{news.get('updated_at', '')}`" if news.get("updated_at") else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{hot_str}{updated}\n{summary}\n[🔗 查看原文]({news['url']})\n\n"

        md += "\n**▸ 热门作品（转发点赞最多）**\n\n"
        for i, news in enumerate(douyin_views, 6):
            hot_str = f" `🔥{news.get('hot_display', '')}`" if news.get("hot_display") else ""
            updated = f" `🕒{news.get('updated_at', '')}`" if news.get("updated_at") else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{hot_str}{updated}\n{summary}\n[🔗 查看原文]({news['url']})\n\n"

        md += "---\n\n"

    # 头条部分
    toutiao_news = [n for n in all_news if n["source"] == "toutiao"]
    if toutiao_news:
        md += "## 📰 今日头条热榜（按热��排序）\n\n"
        for i, news in enumerate(toutiao_news, 1):
            updated = f" `🕒{news.get('updated_at', '')}`" if news.get("updated_at") else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{updated}\n{summary}\n[🔗 查看原文]({news['url']})\n\n"

        md += "---\n\n"

    # GitHub 近15天
    github_news = [n for n in all_news if n["source"] == "github"]
    if github_news:
        md += "## ⭐ GitHub 热门项目（近15天 star 最多）\n\n"
        for i, news in enumerate(github_news, 1):
            star_str = f" `⭐{news.get('hot_display', '')}`" if news.get("hot_display") else ""
            lang_str = f" `{news['language']}`" if news.get("language") else ""
            pushed = f" `🕒{news.get('pushed_at_display', '')}`" if news.get("pushed_at_display") else ""
            summary = summaries.get(news["title"], news.get("description", news["title"]))
            md += f"**{i}. {news['title']}**{star_str}{lang_str}{pushed}\n{summary}\n[🔗 查看仓库]({news['url']})\n\n"

        md += "---\n\n"

    # GitHub 年度热门
    github_yearly = [n for n in all_news if n["source"] == "github_yearly"]
    if github_yearly:
        year = datetime.now().year
        md += f"## 🏆 GitHub 年度���门（{year} star 最多）\n\n"
        for i, news in enumerate(github_yearly, 1):
            star_str = f" `⭐{news.get('hot_display', '')}`" if news.get("hot_display") else ""
            lang_str = f" `{news['language']}`" if news.get("language") else ""
            pushed = f" `🕒{news.get('pushed_at_display', '')}`" if news.get("pushed_at_display") else ""
            summary = summaries.get(news["title"], news.get("description", news["title"]))
            md += f"**{i}. {news['title']}**{star_str}{lang_str}{pushed}\n{summary}\n[🔗 查看仓库]({news['url']})\n\n"

        md += "---\n\n"

    md += "*数据来源：抖音热点榜、今日头条热榜、GitHub Search API | GitHub Actions 自动抓取去重生成*\n"

    return md


# ============================================================
# PushPlus 推送
# ============================================================

def send_to_wechat(content):
    if not PUSHPLUS_TOKEN:
        log("错误: PUSHPLUS_TOKEN 未配置")
        print("ERROR: PUSHPLUS_TOKEN environment variable is not set")
        return False

    today = datetime.now().strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    weekday = weekdays[datetime.now().weekday()]
    title = f"📰 每日新闻日报 - {today} 周{weekday}"

    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "markdown",
        "channel": "wechat"
    }

    try:
        result = http_post_json("http://www.pushplus.plus/send", payload, timeout=30)
        code = result.get("code", -1)
        msg = result.get("msg", "")

        if code == 200:
            log(f"推送成功! {msg}")
            return True
        else:
            log(f"推送失败! code={code}, msg={msg}")
            print(f"ERROR: Push failed - code={code}, msg={msg}")
            print(f"Response: {json.dumps(result, ensure_ascii=False)}")
            return False
    except Exception as e:
        log(f"推送异常: {e}")
        print(f"ERROR: Push exception - {e}")
        return False


def cleanup_intermediate():
    """清理中间文件"""
    final_file = os.path.join(BASE_DIR, "news_final.md")
    if os.path.exists(final_file):
        os.remove(final_file)
        log(f"已清理中间文件: {final_file}")


# ============================================================
# 主流程
# ============================================================

def main():
    log("=" * 60)
    log("GitHub Actions ��日新闻工作流启动")
    log(f"PushPlus Token: {'已配置' if PUSHPLUS_TOKEN else '未配置'}")
    log(f"DeepSeek API Key: {'已配置' if DEEPSEEK_API_KEY else '未配置（将使用标题/description）'}")
    log("=" * 60)

    if not PUSHPLUS_TOKEN:
        print("ERROR: PUSHPLUS_TOKEN is not set. Please configure it in GitHub Secrets.")
        sys.exit(1)

    # 验证 DeepSeek API Key 是否可用
    if DEEPSEEK_API_KEY:
        log("正在验证 DeepSeek API Key...")
        test_content = _deepseek_chat("请回复'OK'两个字母", max_tokens=10, timeout=15, batch_label="（验证）")
        if test_content:
            log(f"DeepSeek API Key 验证通过，返回: {test_content[:20]}")
        else:
            log("⚠️ DeepSeek API Key 验证失败！将使用本地兜底（标题/description）")
            log("⚠️ 请检查 GitHub Secrets 中 DEEPSEEK_API_KEY 是否正确")
    else:
        log("未配置 DEEPSEEK_API_KEY，将使用标题/description 本地兜底")

    history = load_history()
    log(f"已加载历史记录: {len(history.get('sent_titles', []))} 条已发送标题")

    all_news = []

    for source_key, source_config in SOURCES.items():
        source_type = source_config["type"]
        source_name = source_config["name"]
        sort_by = source_config.get("sort_by", "index")

        # GitHub 热门项目（近15天）：渐进式日期扩展
        if source_type == "github":
            gh_count = source_config.get("count", 10)
            days_ranges = [15, 30, 45, 60, 90]
            all_fetched_repos = []
            seen_titles = set()
            total_fetched = 0

            for days in days_ranges:
                if len(all_fetched_repos) >= gh_count * 3:
                    break
                batch = fetch_github_trending(days=days, count=gh_count * 2)
                total_fetched += len(batch)
                for repo in batch:
                    if repo["title"] not in seen_titles:
                        seen_titles.add(repo["title"])
                        all_fetched_repos.append(repo)
                log(f"  近{days}天累计获取 {len(all_fetched_repos)} 个独立项目")
                if len(all_fetched_repos) >= gh_count * 2:
                    break

            unique_news = []
            deduped_news = []
            dup_count = 0
            cross_dup = 0

            for news in all_fetched_repos:
                if is_duplicate(news["title"], history):
                    dup_count += 1
                    deduped_news.append(news)
                    continue
                cross_dup_flag = False
                for existing in all_news:
                    if titles_similar(news["title"], existing["title"]):
                        cross_dup_flag = True
                        break
                if cross_dup_flag:
                    cross_dup += 1
                    continue
                unique_news.append(news)
                if len(unique_news) >= gh_count:
                    break

            if len(unique_news) < gh_count and deduped_news:
                log(f"  去重后仅 {len(unique_news)} 个，从历史去重池补充...")
                for news in deduped_news:
                    if len(unique_news) >= gh_count:
                        break
                    if any(n["title"] == news["title"] for n in unique_news):
                        continue
                    unique_news.append(news)

            log(f"{source_name}: 累计抓取 {total_fetched} 个(独立 {len(all_fetched_repos)}), 历史去重 {dup_count} 个, 跨来源去重 {cross_dup} 个, 保留 {len(unique_news)} 个")

            for news in unique_news:
                news["source_name"] = source_name
                news["source_emoji"] = source_config.get("emoji", "")
                news["hot_display"] = format_hot_value(news.get("stars", 0))
                news["pushed_at_display"] = format_github_date(news.get("pushed_at", ""))
                readme = fetch_github_readme(news)
                news["readme_excerpt"] = readme
                if readme:
                    log(f"  已获取 README: {news['title']}")
                else:
                    log(f"  README 获取失败，使用 description: {news['title']}")

            all_news.extend(unique_news)
            continue

        # GitHub 年度热门：不做历史去重
        if source_type == "github_yearly":
            gh_yearly_count = source_config.get("count", 15)
            news_list = fetch_github_yearly_top(count=gh_yearly_count)
            unique_news = news_list[:gh_yearly_count]

            log(f"{source_name}: 抓取 {len(news_list)} 个, 保留 {len(unique_news)} 个（不做历史去重）")

            for news in unique_news:
                news["source_name"] = source_name
                news["source_emoji"] = source_config.get("emoji", "")
                news["hot_display"] = format_hot_value(news.get("stars", 0))
                news["pushed_at_display"] = format_github_date(news.get("pushed_at", ""))
                readme = fetch_github_readme(news)
                news["readme_excerpt"] = readme
                if readme:
                    log(f"  已获取 README: {news['title']}")
                else:
                    log(f"  README 获取失败，使用 description: {news['title']}")

            all_news.extend(unique_news)
            continue

        # 抖音和头条
        news_list = fetch_hotlist(source_type, sort_by)

        # 抖音特殊处理：双排序
        if source_type == "douyin":
            unique_news = []
            dup_count = 0
            cross_dup = 0
            for news in news_list:
                if is_duplicate(news["title"], history):
                    dup_count += 1
                    continue
                cross_dup_flag = False
                for existing in all_news:
                    if titles_similar(news["title"], existing["title"]):
                        cross_dup_flag = True
                        break
                if cross_dup_flag:
                    cross_dup += 1
                    continue
                unique_news.append(news)

            hot_sorted = sorted(unique_news, key=lambda x: x["hot_value"], reverse=True)
            top_hot = hot_sorted[:5]
            hot_titles = {n["title"] for n in top_hot}
            remaining = [n for n in unique_news if n["title"] not in hot_titles]
            view_sorted = sorted(remaining, key=lambda x: x.get("view_count", 0), reverse=True)
            top_views = view_sorted[:5]
            final_douyin = top_hot + top_views

            for i, news in enumerate(final_douyin):
                news["source_name"] = source_name
                news["source_emoji"] = source_config.get("emoji", "")
                news["hot_display"] = format_hot_value(news["hot_value"])
                news["douyin_type"] = "最新最热" if i < 5 else "热门作品"

            log(f"{source_name}: 抓取 {len(news_list)} 条, 历史去重 {dup_count} 条, 跨来源去重 {cross_dup} 条, 最新最热 {len(top_hot)} 条 + 热门作品 {len(top_views)} 条 = 保留 {len(final_douyin)} 条")
            all_news.extend(final_douyin)
        else:
            unique_news = []
            dup_count = 0
            cross_dup = 0
            for news in news_list:
                if is_duplicate(news["title"], history):
                    dup_count += 1
                    continue
                cross_dup_flag = False
                for existing in all_news:
                    if titles_similar(news["title"], existing["title"]):
                        cross_dup_flag = True
                        break
                if cross_dup_flag:
                    cross_dup += 1
                    continue
                unique_news.append(news)
                if len(unique_news) >= NEWS_COUNT_PER_SOURCE:
                    break

            log(f"{source_name}: 抓取 {len(news_list)} 条, 历史去重 {dup_count} 条, 跨来源去重 {cross_dup} 条, 保留 {len(unique_news)} 条")

            for news in unique_news:
                news["source_name"] = source_name
                news["source_emoji"] = source_config.get("emoji", "")
                news["hot_display"] = format_hot_value(news["hot_value"])

            all_news.extend(unique_news)

    if not all_news:
        log("错误: 没有获取到任何新闻")
        print("ERROR: No news fetched")
        sys.exit(1)

    log(f"共 {len(all_news)} 条新闻待处理")

    # AI 归纳总结
    news_items = [n for n in all_news if n["source"] not in ("github", "github_yearly")]
    github_items = [n for n in all_news if n["source"] == "github"]
    github_yearly_items = [n for n in all_news if n["source"] == "github_yearly"]

    summaries = ai_summarize(news_items)

    all_github = github_items + github_yearly_items
    github_summaries = ai_explain_github(all_github)
    summaries.update(github_summaries)

    # 生成 Markdown
    markdown = generate_markdown(all_news, summaries)

    output_file = os.path.join(BASE_DIR, "news_final.md")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(markdown)
    log(f"Markdown 已保存到 {output_file}")

    # 推送到微信
    success = send_to_wechat(markdown)

    if success:
        # 更新历��记录（年度热门不加入历史记录）
        today = datetime.now().strftime("%Y-%m-%d")
        added_count = 0
        skipped_yearly = 0
        for news in all_news:
            if news.get("source") == "github_yearly":
                skipped_yearly += 1
                continue
            history["records"].append({
                "date": today,
                "title": news["title"],
                "normalized_title": normalize_title(news["title"]),
                "url": news.get("url", ""),
                "source": news.get("source", "")
            })
            added_count += 1
        save_history(history)
        if skipped_yearly:
            log(f"已更新历史记录: 新增 {added_count} 条（跳过 {skipped_yearly} 条年度热门）")
        else:
            log(f"已更新历史记录: 新增 {added_count} 条")

        # 清理中间文件
        cleanup_intermediate()
        log("工作流执行完成，历史记录已更新，中间文件已清理")
    else:
        log("推送失败，历史记录未更新（下次运行会重新尝试）")
        sys.exit(1)


if __name__ == "__main__":
    main()
