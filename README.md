# GitHub Actions 每日新闻日报

自动化抓取五大数据源 + 近5天搜索关键词排行，AI 总结翻译后推送到微信。纯云端运行，电脑不用开机。

## 功能概述

| 数据源 | 数量 | 说明 |
|--------|------|------|
| 🔥 抖音热点榜 | 10条 | 前5最新最热 + 后5热门作品（转发点赞最多） |
| 📰 今日头条热榜 | 10条 | 按热度排序 |
| 📕 小红书热点榜 | 10条 | 前5最新最热 + 后5热门（按热度排序） |
| ⭐ GitHub 近15天热门 | 10个 | 渐进式日期扩展（15→30→45→60→90天）确保满10条 |
| 🏆 GitHub 年度热门 | 15个 | 当前年份 star 最多，不做历史去重 |
| 🔍 搜索关键词排行 | 18词 | 抖音/头条/小红书各 top 6，统计近5天出现频次 |

每条新闻包含 **更新时间**，每个 GitHub 项目包含 **最后推送日期** 和 **README 摘要**（前4000字，自动清洗格式噪音）。

## 工作原理

GitHub Actions 在云端服务器上每天定时运行 8 次 Python 脚本，自动抓取新闻、去重、AI 总结翻译、推送到微信。推送成功后自动提交历史记录回仓库，中间文件自动清理。

```
抓取数据源 → 30天历史去重 + 跨来源去重 → 获取 GitHub README 摘要
    → DeepSeek AI 总结新闻（≤50字）
    → DeepSeek 批量翻译英文项目描述（Google Translate 兜底）
    → 抓取搜索关键词（QSLO/60s）→ 近5天频次统计
    → 生成 Markdown 日报 → 推送微信 / QQ 邮箱（双通道可选）→ 提交历史记录
```

## 文件结构

```
news-daily/
├── news_cloud.py                      # 主脚本：抓取+去重+README清洗+AI总结+关键词统计+推送
├── .github/workflows/news-daily.yml   # GitHub Actions 定时任务配置（每天8次）
├── history.json                       # 历史去重记录 + 搜索关键词积累（运行后自动提交回仓库）
├── news_final.md                      # 当次生成的新闻日报（推送成功后自动清理）
└── README.md                          # 本文件
```

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
2. 把 `news_cloud.py` 拖进去
3. 点 **Commit changes**

**创建 .github/workflows/news-daily.yml：**
1. 点 **Add file** → **Create new file**
2. 文件名输入：`.github/workflows/news-daily.yml`
3. 把本地 `.github/workflows/news-daily.yml` 的内容粘贴进去
4. 点 **Commit changes**

### 第4步：配置 Secrets（密钥）

在仓库页面：

1. 点 **Settings** → 左侧 **Secrets and variables** → **Actions**
2. 点 **New repository secret**

**添加 PushPlus Token（可选，原有推送通道）：**
- Name: `PUSHPLUS_TOKEN`
- Secret: 你的 PushPlus token
- 点 **Add secret**
- 说明：保留的原微信推送通道。**PushPlus 与 QQ 邮箱至少配置一个即可**。

**添加 QQ 邮箱推送（可选，新增通道）：**
- Name: `QQ_EMAIL` → 你的 QQ 邮箱（如 `123456@qq.com`）
- Name: `QQ_AUTH_CODE` → 邮箱 SMTP 授权码（**不是登录密码**；QQ 邮箱网页版 → 设置 → 账户 → 开启 IMAP/SMTP 服务后获取）
- Name: `QQ_TO_EMAIL`（可选）→ 收件人，留空则发给自己
- 点 **Add secret**
- 说明：邮件正文为 **HTML 排版**（自动把 Markdown 转成带样式的网页，已内置纯文本兜底）。依赖 `markdown` 包，workflow 已自动 `pip install markdown`，无需本地安装。

**添加 DeepSeek API Key（可选，用于AI归纳总结+翻译）：**
- Name: `DEEPSEEK_API_KEY`
- Secret: 你的 DeepSeek API Key（见下方说明）
- 点 **Add secret**

> 如果不配置 DEEPSEEK_API_KEY：
> - 新闻总结直接使用原标题
> - 英文 GitHub 项目描述通过 Google Translate 免费接口翻译
>
> 配置后：DeepSeek 对每条新闻做 ≤50字 归纳总结，英文描述批量翻译（10个/批），质量更高。

### 第5步：启用 Actions

1. 点仓库顶部 **Actions** 标签
2. 如果提示，点 **I understand my workflows, go ahead and enable them**
3. 左侧应能看到「每日新闻日报推送」工作流

### 第6步：手动测试

1. 在 Actions 页面，点左侧「每日新闻日报推送」
2. 点右侧 **Run workflow** → **Run workflow**
3. 等待约1-2分钟，点进运行记录查看日志
4. 如果看到「推送成功」，检查微信是否收到消息

## DeepSeek API Key 获取方法（可选）

DeepSeek 是国产AI大模型，注册即送免费额度，每次推送消耗不到0.01元。

1. 访问 https://platform.deepseek.com/
2. 注册登录（支持手机号）
3. 左侧菜单 → **API Keys**
4. 点 **Create API Key**
5. 复制生成的 key（以 `sk-` 开头）
6. 填入 GitHub Secrets 的 `DEEPSEEK_API_KEY`

> **模型说明**：当前使用 `deepseek-v4-flash`（非思考模式）。原 `deepseek-chat` 已于 2026-07-24 停用，已自动迁移。

## 运行时间

- **自动运行**：每天北京时间 8 次
  - 08:05 / 10:05 / 12:05 / 14:05 / 16:05 / 18:05 / 20:05 / 22:05
  - GitHub Actions cron 可能有 5-15 分钟延迟
- **手动运行**：Actions 页面 → Run workflow
- **运行环境**：ubuntu-latest + Python 3.11

## 修改推送时间

编辑 `.github/workflows/news-daily.yml` 中的 cron 表达式：

```yaml
# 北京时间 08:05 / 10:05 / 12:05 / 14:05 / 16:05 / 18:05 / 20:05 / 22:05 八次推送
# UTC = 北京时间 - 8小时
- cron: '5 0 * * *'     # 北京 08:05
- cron: '5 2 * * *'     # 北京 10:05
- cron: '5 4 * * *'     # 北京 12:05
- cron: '5 6 * * *'     # 北京 14:05
- cron: '5 8 * * *'     # 北京 16:05
- cron: '5 10 * * *'    # 北京 18:05
- cron: '5 12 * * *'    # 北京 20:05
- cron: '5 14 * * *'    # 北京 22:05
```

## 去重机制

- **30天历史去重**：同一标题30天内不重复推送（标题归一化 + 前缀/包含匹配）
- **跨来源去重**：抖音和头条有相同内容时只保留一条
- **渐进式日期扩展**：GitHub 近15天不足时自动扩展到 30→45→60→90 天
- **去重池补充**：三个月都耗尽后，从历史已推送项目中重新选取
- **年度热门不做去重**：GitHub 年度热门每次都是 Top 15，不加入历史记录

## AI 功能详解

### 新闻归纳总结（DeepSeek）

对抖音和头条的每条新闻标题做 ≤50字 归纳总结，批量请求，返回 JSON 格式。未配置 API Key 时直接使用原标题。

### GitHub 项目说明翻译

GitHub 项目的 About(description) 处理逻辑：
- **含中文** → 直接使用
- **纯英文** → DeepSeek 批量翻译（10个/批），失败则 Google Translate 兜底
- **无描述** → 使用仓库名

### GitHub README 摘要清洗

通过 `raw.githubusercontent.com` 获取项目 README，自动清洗为纯文本摘要（前4000字）：
- 去除 HTML 标签、图片、链接、代码块
- 过滤多语言导航行、License 信息、徽章、赞助信息、统计表格等噪音
- 保留核心文字内容，用于日志参考

## 搜索关键词排行

每次运行自动抓取抖音、今日头条、小红书的实时热搜关键词（数据来自 QSLO/60s 免费 API），存储到 `history.json`。

- **近5天频次统计**：统计最近5天每个关键词在各平台出现的次数
- **Top 6 排行**：每个平台取出现频次最高的前6个关键词，按「出现次数 + 最高热度」排序
- **自动积累**：每天8次运行自动积累数据，第2天起就能看到趋势排行
- **90天自动清理**：超过90天的关键词数据自动清除，避免 `history.json` 膨胀

## 注意事项

- 仓库必须设为 **Private**，避免泄露 token / 邮箱授权码
- PushPlus token、QQ 邮箱授权码、DeepSeek key 都通过 GitHub Secrets 存储，不会泄露
- 如果某天推送失败，历史记录不会更新，下次运行会重新尝试
- `news_final.md` 是中间文件，推送成功后自动清理；如果仓库中存在该文件，说明上次推送可能失败
- GitHub Actions 免费额度：公开仓库无限，私有仓库每月2000分钟（本任务每天约8分钟）
