#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Actions 版每日新闻工作流
================================
在 GitHub Actions 中运行，无需本地电脑开机。

功能：
  1. 从 uapis.cn 抓取抖音热榜（按热度排序）+ 头条热榜（按排名排序）
  2. 与历史记录比对去重（30天）+ 跨来源去重
  3. 可选：用 DeepSeek AI 对每条新闻做 ≤50字 归纳总结
  4. 生成 Markdown 日报
  5. 通过 PushPlus 推送到微信
  6. 更新历史记录文件（由 GitHub Actions 自动提交回仓库）

环境变量（在 GitHub Secrets 中配置）：
  PUSHPLUS_TOKEN    - PushPlus 推送 token（必需）
  DEEPSEEK_API_KEY  - DeepSeek API 密钥（可选，没有则直接使用标题）
"""

import json
import os
import sys
import re
import time
import urllib.request
import urllib.parse
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
        "sort_by": "index"
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_title(title):
    t = title.strip()
    t = re.sub(r'[\s\u3000\u00a0]+', '', t)
    t = re.sub(r'[\u3000-\u303f\uff00-\uffef，。！？、；：\u201c\u201d\u2018\u2019（）【】《》,.!?:;"\'()\[\]<>]', '', t)
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
            "index": item.get("index", 0),
            "source": source_type
        }
        news_list.append(news)

    if sort_by == "hot_value" and any(n["hot_value"] for n in news_list):
        news_list.sort(key=lambda x: x["hot_value"], reverse=True)
    else:
        news_list.sort(key=lambda x: x.get("index", 999))

    log(f"成功抓取 {source_type} 热榜 {len(news_list)} 条")
    return news_list


# ============================================================
# AI 归纳总结（DeepSeek）
# ============================================================

def ai_summarize(news_list):
    """
    用 DeepSeek API 对新闻列表做 ≤50字 归纳总结。
    一次 API 调用处理所有新闻，节省 token。
    如果没有 API key，直接返回原标题作为总结。
    """
    if not DEEPSEEK_API_KEY:
        log("未配置 DEEPSEEK_API_KEY，直接使用标题作为总结")
        return {n["title"]: n["title"] for n in news_list}

    log(f"正在用 DeepSeek AI 对 {len(news_list)} 条新闻做归纳总结...")

    # 构造提示词
    news_lines = []
    for i, news in enumerate(news_list, 1):
        news_lines.append(f"{i}. {news['title']}")

    prompt = f"""请对以下{len(news_list)}条新闻标题逐一做归纳总结，每条总结不超过50个中文字。
要求：准确概括核心内容，简洁有力，不要添加标题中没有的信息。

新闻列表：
{chr(10).join(news_lines)}

请严格按以下JSON格式返回，不要包含任何其他文字：
{{"summaries": ["总结1", "总结2", ...]}}"""

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 2000
    }

    try:
        result = http_post_json(
            "https://api.deepseek.com/chat/completions",
            payload,
            timeout=60,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
        )
        content = result["choices"][0]["message"]["content"].strip()

        # 尝试解析 JSON
        # 处理可能的 markdown 代码块包裹
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
                # 确保不超过50字
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


# ============================================================
# 生成 Markdown
# ============================================================

def generate_markdown(all_news, summaries):
    today = datetime.now().strftime("%Y年%m月%d日")
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    weekday = weekdays[datetime.now().weekday()]

    md = f"# 📰 每日新闻日报\n\n"
    md += f"> {today} 周{weekday} | 抖音{NEWS_COUNT_PER_SOURCE}条 + 头条{NEWS_COUNT_PER_SOURCE}条\n\n"
    md += "---\n\n"

    # 抖音部分
    douyin_news = [n for n in all_news if n["source"] == "douyin"]
    md += "## 🔥 抖音热点榜（按热度排序）\n\n"
    for i, news in enumerate(douyin_news, 1):
        hot_str = f" `🔥{news.get('hot_display', '')}`" if news.get("hot_display") else ""
        summary = summaries.get(news["title"], news["title"])
        md += f"**{i}. {news['title']}**{hot_str}\n{summary}\n\n"

    md += "---\n\n"

    # 头条部分
    toutiao_news = [n for n in all_news if n["source"] == "toutiao"]
    md += "## 📰 今日头条热榜（当日最新热度）\n\n"
    for i, news in enumerate(toutiao_news, 1):
        summary = summaries.get(news["title"], news["title"])
        md += f"**{i}. {news['title']}**\n{summary}\n\n"

    md += "---\n\n"
    md += "*数据来源：抖音热点榜、今日头条热榜 | GitHub Actions 自动抓取去重生成*\n"

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


# ============================================================
# 主流程
# ============================================================

def main():
    log("=" * 60)
    log("GitHub Actions 每日新闻工作流启动")
    log(f"PushPlus Token: {'已配置' if PUSHPLUS_TOKEN else '未配置'}")
    log(f"DeepSeek API Key: {'已配置' if DEEPSEEK_API_KEY else '未配置（将使用标题）'}")
    log("=" * 60)

    if not PUSHPLUS_TOKEN:
        print("ERROR: PUSHPLUS_TOKEN is not set. Please configure it in GitHub Secrets.")
        sys.exit(1)

    # 加载历史记录
    history = load_history()
    log(f"已加载历史记录: {len(history.get('sent_titles', []))} 条已发送标题")

    all_news = []

    for source_key, source_config in SOURCES.items():
        source_type = source_config["type"]
        source_name = source_config["name"]
        sort_by = source_config.get("sort_by", "index")

        news_list = fetch_hotlist(source_type, sort_by)

        # 去重
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
    summaries = ai_summarize(all_news)

    # 生成 Markdown
    markdown = generate_markdown(all_news, summaries)

    # 输出到文件（方便检查）
    output_file = os.path.join(BASE_DIR, "news_final.md")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(markdown)
    log(f"Markdown 已保存到 {output_file}")

    # 推送到微信
    success = send_to_wechat(markdown)

    if success:
        # 更新历史记录
        today = datetime.now().strftime("%Y-%m-%d")
        for news in all_news:
            history["records"].append({
                "date": today,
                "title": news["title"],
                "normalized_title": normalize_title(news["title"]),
                "url": news.get("url", ""),
                "source": news.get("source", "")
            })
        save_history(history)
        log("工作流执行完成，历史记录已更新")
    else:
        log("推送失败，历史记录未更新（下次运行会重新尝试）")
        sys.exit(1)


if __name__ == "__main__":
    main()
