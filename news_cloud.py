#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 版每日新闻工作流
================================
在 GitHub Actions 中运行，无需本地电脑开机。

功能：
  1. 从 uapis.cn 抓取抖音热榜（前5最新最热+后5热门作品）+ 头条热榜（按热度排序）+ 小红书热点榜（前5最新最热+后5热门）
  2. 从 GitHub Search API 抓取近15天热门项目（渐进式扩展确保10条）+ 年度热门15个
  3. 与历史记录比对去重（30天）+ 跨来源去重
  4. 获取 GitHub 项目 README 摘要（前2000字，日志参考）
  5. 可选：用 DeepSeek AI 对每条新闻做 ≤50字 归纳总结
  6. GitHub 项目说明用 About(description)：中文直接用，英文翻译（DeepSeek批量/Google兜底）
  7. 生成 Markdown 日报（含更新时间、项目说明）
  8. 通过 PushPlus 推送到微信
  9. 更新历史记录文件（由 GitHub Actions 自动提交回仓库）

环境变量（在 GitHub Secrets 中配置）：
  PUSHPLUS_TOKEN    - PushPlus 推送 token（必需）
  DEEPSEEK_API_KEY  - DeepSeek API 密钥（可选，用于新闻归纳 + 英文 description 翻译）
"""

import json
import os
import sys
import re
import time
import glob as glob_module
import urllib.request
import urllib.parse
from datetime import datetime, timedelta

# ============================================================
# 配置
# ============================================================

PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
# 注意：deepseek-chat 已于 2026-07-24 停用，迁移至 deepseek-v4-flash（默认非思考模式）
DEEPSEEK_MODEL = "deepseek-v4-flash"

API_BASE = "https://uapis.cn/api/v1/misc/hotboard"
NEWS_COUNT_PER_SOURCE = 10
DEDUP_LOOKBACK_DAYS = 30

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

SOURCES = {
    "douyin": {
        "name": "抖音热点榜",
        "emoji": "[热]",
        "type": "douyin",
        "sort_by": "hot_value"
    },
    "toutiao": {
        "name": "今日头条热榜",
        "emoji": "[新]",
        "type": "toutiao",
        "sort_by": "hot_value"
    },
    "xiaohongshu": {
        "name": "小红书热点榜",
        "emoji": "[红]",
        "type": "xiaohongshu",
        "sort_by": "hot_value"
    },
    "github": {
        "name": "GitHub 热门项目",
        "emoji": "[星]",
        "type": "github",
        "sort_by": "stars",
        "days": 15,
        "count": 10
    },
    "github_yearly": {
        "name": "GitHub 年度热门",
        "emoji": "[冠]",
        "type": "github_yearly",
        "sort_by": "stars",
        "count": 15
    }
}

# QSLO/60s 搜索关键词 API（免费，无需注册）
SEARCH_KEYWORD_API = "https://60s.viki.moe/v2"
SEARCH_KEYWORD_SOURCES = {
    "douyin": {"name": "抖音", "emoji": "[热]", "endpoint": "douyin"},
    "toutiao": {"name": "今日头条", "emoji": "[新]", "endpoint": "toutiao"},
    "xiaohongshu": {"name": "小红书", "emoji": "[红]", "endpoint": "rednote"},
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
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
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
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        msg = f"HTTP {e.code} {e.reason}"
        if body:
            msg += f" | 响应体: {body[:500]}"
        raise RuntimeError(msg) from e


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
        hot_display_raw = ""  # 保留原始热度字符串（如 "920.8w"）
        if isinstance(hot_value, (int, float)):
            hot_num = int(hot_value)
        elif isinstance(hot_value, str):
            # 处理 "920.8w" 格式
            w_match = re.match(r'^([\d.]+)w$', hot_value.strip().lower())
            if w_match:
                hot_num = int(float(w_match.group(1)) * 10000)
                hot_display_raw = hot_value.strip()
            elif hot_value.strip().isdigit():
                hot_num = int(hot_value.strip())
        elif extra.get("hot_value"):
            hot_num = extra.get("hot_value", 0)

        news = {
            "title": title,
            "url": item.get("url", ""),
            "hot_value": hot_num,
            "hot_display_raw": hot_display_raw,
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

def _deepseek_chat(prompt, max_tokens=2000, timeout=60):
    """调用 DeepSeek API（OpenAI 兼容格式），返回 content 字符串；失败返回 None 并记录详细错误"""
    if not DEEPSEEK_API_KEY:
        return None
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "stream": False,
        "thinking": {"type": "disabled"}
    }
    try:
        result = http_post_json(
            "https://api.deepseek.com/chat/completions",
            payload,
            timeout=timeout,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
        )
    except Exception as e:
        log(f"DeepSeek API 调用失败: {e}")
        return None
    if not isinstance(result, dict):
        log(f"DeepSeek API 返回非预期格式: {type(result)}")
        return None
    if "error" in result:
        log(f"DeepSeek API 返回错误对象: {result['error']}")
        return None
    try:
        content = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        log(f"DeepSeek API 返回结构异常: {e} | 原始: {str(result)[:300]}")
        return None
    if not content:
        log("DeepSeek API 返回空内容")
        return None
    return content


def _parse_json_array(content, key):
    """从 DeepSeek 返回的 content 解析 JSON 数组，失败返回 None"""
    if content.startswith("```"):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        log(f"DeepSeek 返回 JSON 解析失败: {e} | 原始前200字: {content[:200]}")
        return None
    if not isinstance(parsed, dict) or key not in parsed:
        log(f"DeepSeek 返回缺少 '{key}' 字段 | 原始前200字: {content[:200]}")
        return None
    arr = parsed[key]
    if not isinstance(arr, list):
        log(f"DeepSeek 返回 '{key}' 非数组 | 原始前200字: {content[:200]}")
        return None
    return arr


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

    content = _deepseek_chat(prompt, max_tokens=2000, timeout=60)
    if content is None:
        log("AI 总结失败: DeepSeek 调用异常，将使用原标题")
        return {n["title"]: n["title"] for n in news_list}

    summaries = _parse_json_array(content, "summaries")
    if summaries is None:
        log("AI 总结失败: 解析异常，将使用原标题")
        return {n["title"]: n["title"] for n in news_list}

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


def _has_chinese(text):
    """判断文本是否包含中文字符"""
    return bool(re.search(r'[\u4e00-\u9fff]', text or ""))


def _translate_google(text, timeout=10):
    """用 Google Translate 免费接口将英文翻译成中文（DeepSeek 不可用时的兜底）"""
    if not text:
        return ""
    try:
        encoded = urllib.parse.quote(text)
        url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh&dt=t&q={encoded}"
        data = http_get(url, timeout=timeout)
        # 返回格式: [[["翻译片段","原文片段",...],...],...]
        translated = "".join(seg[0] for seg in data[0] if seg[0])
        return translated.strip()
    except Exception as e:
        log(f"Google Translate 翻译失败: {e}")
        return ""


def translate_github_descriptions(repos):
    """用 GitHub About(description) 做项目说明：中文直接用，英文翻译成中文

    - description 含中文 → 直接用
    - description 纯英文 → DeepSeek 批量翻译（10个/批），失败则 Google Translate 兜底
    - 无 description → 用仓库名
    """
    if not repos:
        return {}

    result = {}
    need_translate = []

    for repo in repos:
        desc = (repo.get("description") or "").strip()
        title = repo["title"]
        if not desc:
            result[title] = title
        elif _has_chinese(desc):
            result[title] = desc
        else:
            need_translate.append(repo)

    direct_count = len(repos) - len(need_translate)
    log(f"GitHub 项目说明: {direct_count} 个中文/无描述直接用, {len(need_translate)} 个英文待翻译")

    if not need_translate:
        return result

    if DEEPSEEK_API_KEY:
        result.update(_translate_batch_deepseek(need_translate))
    else:
        log("未配置 DEEPSEEK_API_KEY，使用 Google Translate 翻译英文 description")
        for repo in need_translate:
            translated = _translate_google(repo["description"])
            result[repo["title"]] = translated if translated else repo["description"]
            time.sleep(0.3)

    return result


def _translate_batch_deepseek(repos, batch_size=10):
    """用 DeepSeek API 批量翻译英文 description 为中文（10个/批）"""
    result = {}
    total_batches = (len(repos) + batch_size - 1) // batch_size

    for batch_idx in range(total_batches):
        start = batch_idx * batch_size
        batch = repos[start:start + batch_size]
        batch_num = batch_idx + 1

        lines = []
        for i, repo in enumerate(batch, 1):
            lines.append(f"{i}. {repo['description']}")

        prompt = f"""请将以下{len(batch)}条GitHub项目描述从英文翻译成中文。
要求：
- 简洁准确，保持原意，不要添加多余解释
- 翻译后通常不超过50字

描述列表：
{chr(10).join(lines)}

请严格按以下JSON格式返回，不要包含任何其他文字：
{{"translations": ["翻译1", "翻译2", ...]}}"""

        content = _deepseek_chat(prompt, max_tokens=2000, timeout=60)
        if content is None:
            log(f"DeepSeek 翻译批次 {batch_num}/{total_batches} 失败，用 Google Translate 兜底")
            for repo in batch:
                translated = _translate_google(repo["description"])
                result[repo["title"]] = translated if translated else repo["description"]
                time.sleep(0.3)
            continue

        translations = _parse_json_array(content, "translations")
        if translations is None:
            log(f"DeepSeek 翻译批次 {batch_num}/{total_batches} 解析失败，用 Google Translate 兜底")
            for repo in batch:
                translated = _translate_google(repo["description"])
                result[repo["title"]] = translated if translated else repo["description"]
                time.sleep(0.3)
            continue

        for i, repo in enumerate(batch):
            if i < len(translations):
                result[repo["title"]] = translations[i].strip()
            else:
                result[repo["title"]] = repo["description"]

        log(f"DeepSeek 翻译批次 {batch_num}/{total_batches} 完成 ({len(batch)} 条)")

    return result


# ============================================================
# 搜索关键词抓取与统计
# ============================================================

def fetch_search_keywords(source_key):
    """从 QSLO/60s 抓取热搜关键词
    
    Args:
        source_key: SEARCH_KEYWORD_SOURCES 的 key（douyin/toutiao/xiaohongshu）
    
    Returns:
        list: 关键词列表 [{"keyword": "xxx", "hot_value": 123}, ...]
    """
    cfg = SEARCH_KEYWORD_SOURCES.get(source_key)
    if not cfg:
        return []
    
    url = f"{SEARCH_KEYWORD_API}/{cfg['endpoint']}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        
        if data.get("code") != 200:
            log(f"搜索关键词 [{cfg['name']}] 获取失败: {data.get('message', data)}")
            return []
        
        items = data.get("data", [])
        keywords = []
        seen = set()
        for item in items:
            title = item.get("title", "").strip()
            if not title or len(title) > 50:  # 过滤空和超长
                continue
            if title in seen:
                continue
            seen.add(title)
            
            # 热度值：douyin/toutiao 是 hot_value，xiaohongshu 是 score（如"920.8w"）
            hot_val = item.get("hot_value", 0)
            score = item.get("score", "")
            
            if isinstance(score, str) and score:
                w_match = re.match(r'^([\d.]+)w$', score.strip().lower())
                if w_match:
                    hot_val = int(float(w_match.group(1)) * 10000)
                else:
                    try:
                        hot_val = int(float(score))
                    except (ValueError, TypeError):
                        pass
            
            if isinstance(hot_val, int):
                hot_val = int(hot_val)
            
            keywords.append({"keyword": title, "hot_value": hot_val})
            if len(keywords) >= 20:
                break
        
        log(f"搜索关键词 [{cfg['name']}]: 获取 {len(keywords)} 条")
        return keywords
        
    except Exception as e:
        log(f"搜索关键词 [{cfg['name']}] 抓取异常: {e}")
        return []


def accumulate_keywords(history, keywords_data):
    """将本次搜索关键词写入 history.json
    
    Args:
        history: 当前历史记录字典
        keywords_data: {source_key: [{"keyword": "xx", "hot_value": 123}, ...]}
    """
    today = datetime.now().strftime("%Y-%m-%d")
    
    if "search_keywords" not in history:
        history["search_keywords"] = []
    
    for source_key, kw_list in keywords_data.items():
        if not kw_list:
            continue
        cfg = SEARCH_KEYWORD_SOURCES.get(source_key, {})
        for kw in kw_list:
            history["search_keywords"].append({
                "date": today,
                "source": source_key,
                "source_name": cfg.get("name", source_key),
                "keyword": kw["keyword"],
                "hot_value": kw["hot_value"]
            })
    
    # 清理 90 天前的数据，避免文件膨胀
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    history["search_keywords"] = [
        k for k in history["search_keywords"]
        if k["date"] >= cutoff
    ]
    
    return history


def get_5day_trending(history, top_n=6):
    """统计近5天各平台热搜关键词频次，返回 top N
    
    Returns:
        dict: {
            "total_days": 实际统计天数,
            "platforms": {source_key: [(keyword, count, max_hot_value), ...]}
        }
    """
    cutoff = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    
    search_kws = history.get("search_keywords", [])
    
    # 按平台分组统计
    platform_stats = {}  # {source_key: {keyword: {"count": n, "max_hot": v}}}
    
    for entry in search_kws:
        if entry["date"] < cutoff:
            continue
        sk = entry["source"]
        kw = entry["keyword"]
        hv = entry.get("hot_value", 0)
        
        if sk not in platform_stats:
            platform_stats[sk] = {}
        
        if kw not in platform_stats[sk]:
            platform_stats[sk][kw] = {"count": 0, "max_hot": 0}
        
        platform_stats[sk][kw]["count"] += 1
        platform_stats[sk][kw]["max_hot"] = max(platform_stats[sk][kw]["max_hot"], hv)
    
    # 排序取 top N + 计算实际统计天数
    platforms = {}
    date_set = set()
    for sk, kw_dict in platform_stats.items():
        sorted_kws = sorted(
            kw_dict.items(),
            key=lambda x: (x[1]["count"], x[1]["max_hot"]),
            reverse=True
        )
        platforms[sk] = [
            (kw, stats["count"], stats["max_hot"])
            for kw, stats in sorted_kws[:top_n]
        ]
    
    # 统计实际覆盖天数
    for entry in search_kws:
        if entry["date"] >= cutoff:
            date_set.add(entry["date"])
    
    return {
        "total_days": max(len(date_set), 1),
        "platforms": platforms
    }


# ============================================================
# 生成 Markdown
# ============================================================

def generate_markdown(all_news, summaries, trending_keywords=None):
    today = datetime.now().strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    weekday = weekdays[datetime.now().weekday()]

    douyin_count = sum(1 for n in all_news if n["source"] == "douyin")
    toutiao_count = sum(1 for n in all_news if n["source"] == "toutiao")
    xhs_count = sum(1 for n in all_news if n["source"] == "xiaohongshu")
    github_count = sum(1 for n in all_news if n["source"] == "github")
    github_yearly_count = sum(1 for n in all_news if n["source"] == "github_yearly")

    md = f"# [日报] 每日新闻日报\n\n"
    md += f"> {today} 周{weekday} | 抖音{douyin_count}条 + 头条{toutiao_count}条 + 小红书{xhs_count}条 + GitHub近15天{github_count}个 + 年度热门{github_yearly_count}个\n\n"
    md += "---\n\n"

    # 抖音部分
    douyin_news = [n for n in all_news if n["source"] == "douyin"]
    douyin_hot = [n for n in douyin_news if n.get("douyin_type") == "最新最热"]
    douyin_views = [n for n in douyin_news if n.get("douyin_type") == "热门作品"]

    if douyin_news:
        md += "## [热] 抖音热点榜\n\n"
        md += "**▸ 最新最热（按综合热度排序）**\n\n"
        for i, news in enumerate(douyin_hot, 1):
            hot_str = f" [热]{news.get('hot_display', '')}" if news.get('hot_display') else ""
            updated = f" [更]{news.get('updated_at', '')}" if news.get('updated_at') else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{hot_str}{updated}\n{summary}\n[查看原文]({news['url']})\n\n"

        md += "\n**▸ 热门作品（转发点赞最多）**\n\n"
        for i, news in enumerate(douyin_views, 6):
            hot_str = f" [热]{news.get('hot_display', '')}" if news.get('hot_display') else ""
            updated = f" [更]{news.get('updated_at', '')}" if news.get('updated_at') else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{hot_str}{updated}\n{summary}\n[查看原文]({news['url']})\n\n"

        md += "---\n\n"

    # 头条部分
    toutiao_news = [n for n in all_news if n["source"] == "toutiao"]
    if toutiao_news:
        md += "## [新] 今日头条热榜（按热度排序）\n\n"
        for i, news in enumerate(toutiao_news, 1):
            updated = f" [更]{news.get('updated_at', '')}" if news.get('updated_at') else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{updated}\n{summary}\n[查看原文]({news['url']})\n\n"

        md += "---\n\n"

    # 小红书部分
    xhs_news = [n for n in all_news if n["source"] == "xiaohongshu"]
    xhs_hot = [n for n in xhs_news if n.get("douyin_type") == "最新最热"]
    xhs_views = [n for n in xhs_news if n.get("douyin_type") == "热门作品"]

    if xhs_news:
        md += "## [红] 小红书热点榜\n\n"
        md += "**▸ 最新最热（按综合热度排序）**\n\n"
        for i, news in enumerate(xhs_hot, 1):
            hot_str = f" [红]{news.get('hot_display', '')}" if news.get('hot_display') else ""
            updated = f" [更]{news.get('updated_at', '')}" if news.get('updated_at') else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{hot_str}{updated}\n{summary}\n[查看原文]({news['url']})\n\n"

        md += "\n**▸ 热门作品（按热度排序）**\n\n"
        for i, news in enumerate(xhs_views, 6):
            hot_str = f" [红]{news.get('hot_display', '')}" if news.get('hot_display') else ""
            updated = f" [更]{news.get('updated_at', '')}" if news.get('updated_at') else ""
            summary = summaries.get(news["title"], news["title"])
            md += f"**{i}. {news['title']}**{hot_str}{updated}\n{summary}\n[查看原文]({news['url']})\n\n"

        md += "---\n\n"

    # GitHub 近15天
    github_news = [n for n in all_news if n["source"] == "github"]
    if github_news:
        md += "## [星] GitHub 热门项目（近15天 star 最多）\n\n"
        for i, news in enumerate(github_news, 1):
            star_str = f" [星]{news.get('hot_display', '')}" if news.get("hot_display") else ""
            lang_str = f" [{news['language']}]" if news.get("language") else ""
            pushed = f" [更]{news.get('pushed_at_display', '')}" if news.get("pushed_at_display") else ""
            summary = summaries.get(news["title"], news.get("description", news["title"]))
            md += f"**{i}. {news['title']}**{star_str}{lang_str}{pushed}\n{summary}\n[查看仓库]({news['url']})\n\n"

        md += "---\n\n"

    # GitHub 年度热门
    github_yearly = [n for n in all_news if n["source"] == "github_yearly"]
    if github_yearly:
        year = datetime.now().year
        md += f"## [冠] GitHub 年度热门（{year} star 最多）\n\n"
        for i, news in enumerate(github_yearly, 1):
            star_str = f" [星]{news.get('hot_display', '')}" if news.get("hot_display") else ""
            lang_str = f" [{news['language']}]" if news.get("language") else ""
            pushed = f" [更]{news.get('pushed_at_display', '')}" if news.get("pushed_at_display") else ""
            summary = summaries.get(news["title"], news.get("description", news["title"]))
            md += f"**{i}. {news['title']}**{star_str}{lang_str}{pushed}\n{summary}\n[查看仓库]({news['url']})\n\n"

        md += "---\n\n"

    # 近5天热搜关键词
    if trending_keywords:
        platforms_kw = trending_keywords.get("platforms", {})
        total_days = trending_keywords.get("total_days", 1)
        
        md += "## [搜] 近5天大家都在搜\n\n"
        md += "> 统计最近5天各平台热搜关键词出现频次，取前6个高频词\n\n"

        for sk, kw_list in platforms_kw.items():
            if not kw_list:
                continue
            cfg = SEARCH_KEYWORD_SOURCES.get(sk, {})
            name = cfg.get("name", sk)
            emoji = cfg.get("emoji", "")
            md += f"### {emoji} {name}\n\n"
            md += "| 排名 | 关键词 | 出现次数 | 最高热度 |\n"
            md += "|------|--------|----------|----------|\n"
            for rank, (kw, count, max_hot) in enumerate(kw_list, 1):
                hot_display = format_hot_value(max_hot) if max_hot else "-"
                md += f"| {rank} | {kw} | {count}次 | {hot_display} |\n"
            md += "\n"

        md += f"> 数据统计范围：近{total_days}天 | 关键词来自 QSLO/60s 搜索热榜\n\n"
        md += "---\n\n"

    md += "*数据来源：抖音热点榜、今日头条热榜、小红书热点榜、GitHub Search API | GitHub Actions 自动抓取去重生成*\n"

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
    title = f"[日报] 每日新闻日报 - {today} 周{weekday}"

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

        # 抖音、小红书和头条
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

        # 小红书特殊处理：双排序（无view_count，按hot_value分两段）
        elif source_type == "xiaohongshu":
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
            remaining = [n for n in hot_sorted if n["title"] not in {t["title"] for t in top_hot}]
            top_views = remaining[:5]
            final_xhs = top_hot + top_views

            for i, news in enumerate(final_xhs):
                news["source_name"] = source_name
                news["source_emoji"] = source_config.get("emoji", "")
                news["hot_display"] = news.get("hot_display_raw") or format_hot_value(news["hot_value"])
                news["douyin_type"] = "最新最热" if i < 5 else "热门作品"

            log(f"{source_name}: 抓取 {len(news_list)} 条, 历史去重 {dup_count} 条, 跨来源去重 {cross_dup} 条, 最新最热 {len(top_hot)} 条 + 热门作品 {len(top_views)} 条 = 保留 {len(final_xhs)} 条")
            all_news.extend(final_xhs)
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
    github_summaries = translate_github_descriptions(all_github)
    summaries.update(github_summaries)

    # 抓取各平台搜索关键词
    log("--- 抓取各平台搜索关键词 ---")
    keywords_data = {}
    for sk in SEARCH_KEYWORD_SOURCES:
        kws = fetch_search_keywords(sk)
        if kws:
            keywords_data[sk] = kws

    # 近5天趋势统计
    trending_keywords = get_5day_trending(history)
    platforms_kw = trending_keywords.get("platforms", {})
    if platforms_kw:
        total_kw = sum(len(v) for v in platforms_kw.values())
        log(f"近5天热搜关键词: {total_kw} 个词（{len(platforms_kw)} 个平台）")
    else:
        log("暂无近5天关键词数据（首次运行或首次积累中）")

    # 生成 Markdown
    markdown = generate_markdown(all_news, summaries, trending_keywords)

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
        # 积累搜索关键词
        if keywords_data:
            history = accumulate_keywords(history, keywords_data)
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