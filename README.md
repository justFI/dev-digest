# dev-digest · 开发资讯日报

每日自动从 Hacker News、DEV Community、Reddit、Lobsters 等渠道采集 **AI、iOS、Android、前端、后端** 等领域的开发技巧、实践经验与行业干货，生成 HTML 页面并发布到 GitHub Pages。

## 在线阅读

部署成功后访问：**https://justfi.github.io/dev-digest/**（以仓库 Pages 设置为准）

## 每条资讯包含

| 字段 | 说明 |
|------|------|
| **title** | 标题 |
| **describe** | 原文摘要/描述 |
| **media** | 图片或媒体 URL 列表 |
| **origin url** | 原文链接 |
| **ai 总结** | AI 生成的中文要点总结 |

## 自动化

- **定时**：每天 UTC 01:00（`cron: 0 1 * * *`）
- **手动**：Actions → Daily Dev Digest → Run workflow
- **输出**：`docs/index.html` + `data/latest.json`

## 本地运行

```bash
pip install -r scripts/requirements.txt
python scripts/collect_digest.py
# 在浏览器打开 docs/index.html
```

### 可选：OpenAI 摘要

在仓库 Settings → Secrets 添加 `OPENAI_API_KEY`，工作流将调用 API 生成更高质量的中文总结。未配置时使用内置规则摘要。

可选环境变量：

- `OPENAI_BASE_URL` / `DIGEST_OPENAI_BASE_URL`
- `OPENAI_MODEL` / `DIGEST_OPENAI_MODEL`（默认 `gpt-4o-mini`）
- `DIGEST_MAX_AGE_HOURS`（默认 48）
- `DIGEST_MAX_PER_CATEGORY`（默认 12）

## 数据源配置

编辑 [`config/sources.yaml`](config/sources.yaml) 可增删 RSS 源或分类。

## GitHub Pages 启用

1. 仓库 **Settings → Pages**
2. Source 选择 **Deploy from a branch**
3. Branch 选 **gh-pages**，目录 **/ (root)**

首次推送后由 `daily-digest.yml` 自动创建 `gh-pages` 分支。
