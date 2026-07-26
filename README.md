# GitHub Actions 每日新闻工作流 - 设置指南

## 功能概述

自动化抓取四大数据源，AI 总结后推送到微信：

| 数据源 | 数量 | 说明 |
|--------|------|------|
| 🔥 抖音热点榜 | 10条 | 前5最新最热 + 后5热门作品（转发点赞最多） |
| 📰 今日头条热榜 | 10条 | 按热度排序 |
| ⭐ GitHub 近15天热门 | 10个 | 渐进式日期扩展（15→30→45→60→90天）确保满10条 |
| 🏆 GitHub 年度热门 | 15个 | 当前年份 star 最多，不做历史去重 |

每条新闻包含 **更新时间**，每个 GitHub 项目包含 **最后推送日期** 和 **README 摘要**。

## 原理

GitHub Actions 在云端服务器上每天定时运行 Python 脚本，自动抓取新闻、去重、推送到微信。你的电脑不需要开机。

---

## 设置步骤

### 第1步：注册 GitHub 账号（已有跳过）

访问 https://github.com/signup 注册，免费。

### 第2步：创建新仓库

1. 登录 GitHub，点右上角 **+** → **New repository**
2. 仓库名填：`news-daily`（随意）
3. 选择 **Private**（私有，别人看不到）
4. 勾选 **Add a README file**
5. 点 **Create repository**

### 第3步：上传脚本文件

在仓库页面操作：

**上传 news_cloud.py：**
1. 点 **Add file** → **Upload files**
2. 把本地 `github_actions/news_cloud.py` 拖进去
3. 点 **Commit changes**

**创建 .github/workflows/news-daily.yml：**
1. 点 **Add file** → **Create new file**
2. 文件名输入：`.github/workflows/news-daily.yml`
3. 把本地 `github_actions/.github/workflows/news-daily.yml` 的内容粘贴进去
4. 点 **Commit changes**

### 第4步：配置 Secrets（密钥）

在仓库页面：

1. 点 **Settings** → 左侧 **Secrets and variables** → **Actions**
2. 点 **New repository secret**

**添加 PushPlus Token（必需）：**
- Name: `PUSHPLUS_TOKEN`
- Secret: `*********************************`
- 点 **Add secret**

**添加 DeepSeek API Key（可选，用于AI归纳总结）：**
- Name: `DEEPSEEK_API_KEY`
- Secret: 你的 DeepSeek API Key（见下方说明）
- 点 **Add secret**

> 如果不配置 DEEPSEEK_API_KEY，脚本会直接使用新闻标题或 GitHub description。配置后 AI 会对每条新闻做更准确的50字归纳总结。

### 第5步：启用 Actions

1. 点仓库顶部 **Actions** 标签
2. 如果提示，点 **I understand my workflows, go ahead and enable them**
3. 左侧应能看到「每日新闻日报推送」工作流

### 第6步：手动测试

1. 在 Actions 页面，点左侧「每日新闻日报推送」
2. 点右侧 **Run workflow** → **Run workflow**
3. 等待约1-2分钟，点进运行记录查看日志
4. 如果看到「推送成功」，检查微信是否收到消息

---

## DeepSeek API Key 获取方法（可选）

DeepSeek 是国产AI大模���，注册即送免费额度，每次推送消耗不到0.01元。

1. 访问 https://platform.deepseek.com/
2. 注册登录（支持手机号）
3. 左侧菜单 → **API Keys**
4. 点 **Create API Key**
5. 复制生成的 key（以 `sk-` 开头）
6. 填入 GitHub Secrets 的 `DEEPSEEK_API_KEY`

---

## 运行时间

- **自动运行**：每天北京时间 8:00（GitHub Actions cron 可能有5-15分钟延迟）
- **手动运行**：Actions 页面 → Run workflow

## 修改推送时间

编辑 `.github/workflows/news-daily.yml` 中的 cron 表达式：

| 北京时间 | cron 表达式 |
|---------|------------|
| 8:00 | `0 0 * * *` |
| 12:00 | `0 4 * * *` |
| 18:00 | `0 10 * * *` |
| 21:00 | `0 13 * * *` |

## 去重机制

- **30天历史去重**：同一标题30天内不重复推送
- **跨来源去重**：抖音和头条有相同内容时只保留一条
- **渐进式日期扩展**：GitHub 近15天不足时自动扩展到30→45→60→90天
- **去重池补充**：三个月都耗尽后，从历史已推送项目中重新选取
- **年度热门不做去重**：GitHub 年度热门每次都是 Top 15，不加入历史记录

## 文件说明

| 文件 | 作用 |
|------|------|
| `news_cloud.py` | 主脚本：抓取+去重+README获取+AI总结+推送 |
| `.github/workflows/news-daily.yml` | GitHub Actions 定时任务配置 |
| `history.json` | 历史去重记录（运行后自动生成并提交） |
| `news_final.md` | 当天生成的新闻日报（推送后自动清理） |

## 两个版本差异

| 特性 | 本地版 (news_workflow.py) | 云端版 (news_cloud.py) |
|------|--------------------------|------------------------|
| 运行环境 | WorkBuddy Automation + 本地 Python | GitHub Actions + 云端 Python |
| 配置方式 | config.json | GitHub Secrets 环境变量 |
| 推送频率 | 每天4次（8/12/18/20点） | 每天1次（8点） |
| AI 总结 | WorkBuddy AI 直接生成 | DeepSeek API（可选） |
| 数据源 | 抖音+头条+GitHub近15天+年度热门 | 完全相同 |
| README 摘要 | ✅ 前2000字 | ✅ 前2000字 |
| 中间文件清理 | push成功后自动清理 | push成功后自动清理 |

## 注意事项

- 仓库必须设为 **Private**，避免泄露 token
- PushPlus token 和 DeepSeek key 都通过 GitHub Secrets 存储，不会泄露
- 如果某天推送失败，历史记录不会更新，下次运行会重新尝试
- GitHub Actions 免费额度：公开仓库无限，私有仓库每月2000分钟（本任务每天约1分钟）
