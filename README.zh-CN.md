# IACR ePrint Monitor

IACR Cryptology ePrint Archive 本地归档与每日监控工具。

本项目把 IACR ePrint 的 OAI-PMH 元数据全量保存到本地 SQLite，并用 SQLite FTS5 建标题、作者、摘要搜索索引。每日任务会拉取 RSS 快照，再用 OAI 增量补漏，最后生成 Markdown 日报。PDF 下载默认关闭，因为 IACR robots 对普通爬虫明确允许 `/oai` 和 `/rss`，但不允许批量抓 PDF。

English documentation: [README.md](README.md)

## 当前部署

- 主机：`r205`
- 项目目录：`/home/ubuntu/projects/iacr-monitor`
- 数据库：`/home/ubuntu/projects/iacr-monitor/data/iacr.db`
- 每日日报：`/home/ubuntu/projects/iacr-monitor/reports/daily/`
- 定时器：`iacr-monitor.timer`
- 当前 active papers：`25722`

## 项目架构

```text
IACR /oai + /rss
      |
      v
scripts/harvest_oai.py     scripts/poll_daily.py
      |                         |
      v                         v
data/raw/oai/              data/raw/rss/
      |                         |
      +----------+--------------+
                 v
            data/iacr.db
                 |
      +----------+-----------+
      |                      |
papers 表             paper_fts FTS5 索引
pdf_assets 表         harvest_runs 表
                 |
                 v
reports/daily/YYYY-MM-DD.md
```

核心文件：

- `scripts/harvest_oai.py`：全量或增量 OAI-PMH 元数据抓取。
- `scripts/poll_daily.py`：每日 RSS 快照 + OAI 增量补漏 + Markdown 日报。
- `scripts/search.py`：本地 FTS5 搜索标题、作者、摘要。
- `scripts/download_pdfs.py`：可选 PDF 队列，默认禁用，并先检查 robots。
- `deploy/systemd/`：每日自动化的 systemd service/timer。
- `data/iacr.db`：SQLite 数据库，不走普通 Git 提交。

## 数据库结构

主要表：

- `papers`：每篇论文一行，包含 `paper_id`、标题、摘要、作者 JSON、分类 JSON、页面 URL、PDF URL、提交/更新时间、license URL、source hash。
- `paper_fts`：SQLite FTS5 索引，覆盖 `paper_id`、标题、摘要、作者。
- `pdf_assets`：PDF 下载队列和本地文件元数据。默认 `pending`，不实际下载 PDF。
- `harvest_runs`：全量/增量抓取运行记录。
- `notifications_sent`：后续推送去重预留表。

## 常用命令

进入项目目录：

```bash
cd /home/ubuntu/projects/iacr-monitor
```

初始化数据库：

```bash
python3 scripts/init_db.py
```

全量抓取 metadata/摘要：

```bash
python3 scripts/harvest_oai.py --mode full
```

手动跑一次每日任务：

```bash
python3 scripts/poll_daily.py
```

小范围测试抓取：

```bash
python3 scripts/harvest_oai.py --mode incremental --from-date 2026-05-01T00:00:00Z --limit-pages 1
```

本地搜索：

```bash
python3 scripts/search.py 'zkAgent' --limit 5
python3 scripts/search.py 'lattice OR accumulator' --limit 10
```

查看最新编号的论文：

```bash
sqlite3 -json data/iacr.db "
SELECT *
FROM papers
WHERE deleted = 0
  AND paper_id LIKE '2026/%'
ORDER BY CAST(SUBSTR(paper_id, INSTR(paper_id, '/') + 1) AS INTEGER) DESC
LIMIT 3;
"
```

检查摘要覆盖率：

```bash
sqlite3 data/iacr.db "
SELECT
  count(*) AS total,
  sum(CASE WHEN trim(coalesce(abstract, '')) = '' THEN 1 ELSE 0 END) AS empty_abstracts,
  min(length(coalesce(abstract, ''))) AS min_len,
  max(length(coalesce(abstract, ''))) AS max_len,
  avg(length(coalesce(abstract, ''))) AS avg_len
FROM papers
WHERE deleted = 0;
"
```

PDF 策略 dry-run：

```bash
python3 scripts/download_pdfs.py --dry-run --limit 5
```

## 每日自动化

安装并启动 timer：

```bash
sudo cp deploy/systemd/iacr-monitor.service /etc/systemd/system/
sudo cp deploy/systemd/iacr-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iacr-monitor.timer
```

查看定时任务：

```bash
systemctl list-timers --all iacr-monitor.timer --no-pager
```

通过 systemd 手动跑一次：

```bash
sudo systemctl start iacr-monitor.service
journalctl -u iacr-monitor.service -n 100 --no-pager
```

默认每天 `01:10 UTC` 跑一次，即上海时间 `09:10`。

## PDF 策略

PDF 下载默认关闭：

```toml
[pdf]
enabled = false
respect_robots = true
```

原因：

- IACR metadata 可以通过 OAI-PMH 和 RSS 合规获取。
- 当前 robots 对普通爬虫允许 `/oai` 和 `/rss`。
- 当前 robots 不允许批量抓 PDF。

可以用 `download_pdfs.py --dry-run` 检查候选 PDF 和 robots 判断。只有在有明确许可或清晰策略依据时，才应开启 PDF 下载。

## GitHub 发布方式

推荐结构：

- 代码、文档、配置模板、systemd 文件进 Git。
- `data/iacr.db` 不进普通 Git；当前 DB 已超过 GitHub 普通文件 `100 MiB` 硬限制。
- 数据库作为 GitHub Release asset 发布，例如 `iacr-db-YYYY-MM-DD.sqlite.gz`。
- 生成数据和日志保持 ignored。

建议 `.gitignore`：

```gitignore
data/
reports/
logs/
*.db
*.db-wal
*.db-shm
```

## 数据库快照

生成压缩数据库：

```bash
mkdir -p releases
gzip -c data/iacr.db > releases/iacr-db-$(date -u +%F).sqlite.gz
sha256sum releases/iacr-db-$(date -u +%F).sqlite.gz > releases/iacr-db-$(date -u +%F).sqlite.gz.sha256
```

使用者恢复：

```bash
gunzip -c iacr-db-YYYY-MM-DD.sqlite.gz > data/iacr.db
```

## 运维注意

- 主抓取面只用 `/oai` 和 `/rss`。
- 保持每日一次频率。
- 长期部署前，在 `config.toml` 里设置真实 `User-Agent` 和可选 `From`。
- 遇到 `403`、`429` 或其他拒绝信号应停止本轮。
- 原始 OAI XML 保存在 `data/raw/oai/` 供审计，但不进 Git。

