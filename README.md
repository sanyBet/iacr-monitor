# IACR ePrint Monitor

Local archiver and daily monitor for the IACR Cryptology ePrint Archive.

This project stores all available IACR ePrint OAI-PMH metadata locally, builds a SQLite FTS5 search index over titles, authors, and abstracts, and runs a daily incremental monitor that writes a Markdown digest. PDF downloads are intentionally disabled by default because IACR robots policy allows `/oai` and `/rss` for generic crawlers, while bulk PDF crawling is not allowed by that policy.

Chinese documentation: [README.zh-CN.md](README.zh-CN.md)

## Current Deployment

- Host: `r205`
- Project directory: `/home/ubuntu/projects/iacr-monitor`
- Database: `/home/ubuntu/projects/iacr-monitor/data/iacr.db`
- Daily reports: `/home/ubuntu/projects/iacr-monitor/reports/daily/`
- Daily timer: `iacr-monitor.timer`
- Current indexed active papers: `25722`

## Architecture

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
papers table          paper_fts FTS5 index
pdf_assets table      harvest_runs table
                 |
                 v
reports/daily/YYYY-MM-DD.md
```

Core components:

- `scripts/harvest_oai.py`: full or incremental OAI-PMH metadata harvesting.
- `scripts/poll_daily.py`: daily RSS snapshot plus OAI incremental catch-up, then Markdown digest generation.
- `scripts/search.py`: local FTS5 search for title, authors, and abstract.
- `scripts/download_pdfs.py`: optional PDF queue, disabled by default and guarded by robots checks.
- `deploy/systemd/`: systemd service and timer for daily automation.
- `data/iacr.db`: SQLite database, not committed to normal Git.

## Database Model

Main tables:

- `papers`: one row per ePrint paper, including `paper_id`, title, abstract, authors JSON, categories JSON, URL, PDF URL, submission/update times, license URL, and source hash.
- `paper_fts`: SQLite FTS5 index over `paper_id`, `title`, `abstract`, and authors.
- `pdf_assets`: PDF download queue and local file metadata. Current default status is `pending`; actual PDF files are not downloaded.
- `harvest_runs`: execution history for full and incremental harvests.
- `notifications_sent`: reserved for future push notification deduplication.

## Common Commands

Run from the project directory:

```bash
cd /home/ubuntu/projects/iacr-monitor
```

Initialize database:

```bash
python3 scripts/init_db.py
```

Run full metadata harvest:

```bash
python3 scripts/harvest_oai.py --mode full
```

Run a daily poll manually:

```bash
python3 scripts/poll_daily.py
```

Run a bounded test harvest:

```bash
python3 scripts/harvest_oai.py --mode incremental --from-date 2026-05-01T00:00:00Z --limit-pages 1
```

Search locally:

```bash
python3 scripts/search.py 'zkAgent' --limit 5
python3 scripts/search.py 'lattice OR accumulator' --limit 10
```

Inspect the newest paper IDs:

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

Check abstract coverage:

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

Run PDF dry-run policy check:

```bash
python3 scripts/download_pdfs.py --dry-run --limit 5
```

## Daily Automation

Install and start the timer:

```bash
sudo cp deploy/systemd/iacr-monitor.service /etc/systemd/system/
sudo cp deploy/systemd/iacr-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iacr-monitor.timer
```

Check schedule:

```bash
systemctl list-timers --all iacr-monitor.timer --no-pager
```

Run once through systemd:

```bash
sudo systemctl start iacr-monitor.service
journalctl -u iacr-monitor.service -n 100 --no-pager
```

The default timer runs daily at `01:10 UTC`, which is `09:10 Asia/Shanghai`.

## PDF Policy

PDF downloads are disabled by default:

```toml
[pdf]
enabled = false
respect_robots = true
```

Reason:

- IACR metadata is available through OAI-PMH and RSS under a clear metadata policy.
- Generic crawler access is allowed for `/oai` and `/rss`.
- Bulk PDF crawling is not enabled by the current robots policy.

Use `download_pdfs.py --dry-run` to inspect what would happen. Only enable PDF downloads if you have a clear permission basis.

## GitHub Publishing

Recommended layout:

- Commit source code, docs, config template, and systemd files.
- Do not commit `data/iacr.db` through normal Git; it is larger than GitHub's 100 MiB regular file limit.
- Publish the database as a GitHub Release asset, for example `iacr-db-YYYY-MM-DD.sqlite.gz`.
- Keep generated data ignored by Git.

Suggested ignored paths:

```gitignore
data/
reports/
logs/
*.db
*.db-wal
*.db-shm
```

## Release Snapshot

Prepare a compressed database snapshot:

```bash
mkdir -p releases
gzip -c data/iacr.db > releases/iacr-db-$(date -u +%F).sqlite.gz
sha256sum releases/iacr-db-$(date -u +%F).sqlite.gz > releases/iacr-db-$(date -u +%F).sqlite.gz.sha256
```

Consumers can restore it:

```bash
gunzip -c iacr-db-YYYY-MM-DD.sqlite.gz > data/iacr.db
```

## Operational Notes

- Use only `/oai` and `/rss` as the primary fetch surfaces.
- Keep polling frequency at once per day.
- Set a real `User-Agent` and optional `From` value in `config.toml` before long-running deployment.
- Stop on `403`, `429`, or other refusal signals.
- Raw OAI XML is retained under `data/raw/oai/` for auditability, but it is not committed.

