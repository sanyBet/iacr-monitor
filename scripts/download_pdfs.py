#!/usr/bin/env python3
"""Optional PDF downloader with robots and license guardrails."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import urllib.robotparser
from pathlib import Path

from lib_iacr import (
    connect_db,
    ensure_dirs,
    fetch_bytes,
    http_config,
    init_db,
    iso_z,
    load_config,
    positive_int,
    resolve_path,
    utc_now,
)


def robots_allows(config: dict, url: str) -> tuple[bool, str]:
    pdf_cfg = config.get("pdf", {})
    if not bool(pdf_cfg.get("respect_robots", True)):
        return True, "robots check disabled by config"
    robots_url = pdf_cfg.get("robots_url", "https://eprint.iacr.org/robots.txt")
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    rp.read()
    agent = http_config(config).user_agent
    allowed = rp.can_fetch(agent, url)
    return allowed, f"robots {'allows' if allowed else 'blocks'} {url}"


def candidate_rows(conn: sqlite3.Connection, mode: str, limit: int) -> list[sqlite3.Row]:
    if mode == "new-only":
        sql = """
        SELECT p.paper_id, p.pdf_url, p.rights
        FROM papers p
        LEFT JOIN pdf_assets a ON a.paper_id = p.paper_id
        WHERE p.deleted = 0 AND coalesce(a.status, 'pending') IN ('pending', 'failed')
        ORDER BY p.last_updated_at DESC
        LIMIT ?
        """
    else:
        sql = """
        SELECT p.paper_id, p.pdf_url, p.rights
        FROM papers p
        LEFT JOIN pdf_assets a ON a.paper_id = p.paper_id
        WHERE p.deleted = 0 AND coalesce(a.status, 'pending') != 'downloaded'
        ORDER BY p.paper_id
        LIMIT ?
        """
    return conn.execute(sql, (limit,)).fetchall()


def mark(conn: sqlite3.Connection, paper_id: str, status: str, message: str, **extra: object) -> None:
    conn.execute(
        """
        INSERT INTO pdf_assets(paper_id, pdf_url, status, message)
        SELECT paper_id, pdf_url, ?, ? FROM papers WHERE paper_id = ?
        ON CONFLICT(paper_id) DO UPDATE SET
          status=excluded.status,
          message=excluded.message
        """,
        (status, message, paper_id),
    )
    if extra:
        assignments = ", ".join(f"{key} = ?" for key in extra)
        conn.execute(
            f"UPDATE pdf_assets SET {assignments} WHERE paper_id = ?",
            (*extra.values(), paper_id),
        )
    conn.commit()


def local_pdf_path(config: dict, paper_id: str) -> Path:
    base = resolve_path(config, "pdfs", "data/pdfs")
    year, number = paper_id.split("/", 1)
    return base / year / f"{number}.pdf"


def main() -> None:
    parser = argparse.ArgumentParser(description="Optionally download IACR PDFs")
    parser.add_argument("--mode", choices=["new-only", "backfill"], help="override config mode")
    parser.add_argument("--limit", type=positive_int, help="max PDFs this run")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load_config()
    ensure_dirs(config)
    pdf_cfg = config.get("pdf", {})
    enabled = bool(pdf_cfg.get("enabled", False))
    mode = args.mode or pdf_cfg.get("mode", "new-only")
    limit = args.limit or int(pdf_cfg.get("max_per_run", 10))

    conn = connect_db(config)
    init_db(conn)
    rows = candidate_rows(conn, mode, limit)
    if not enabled and not args.dry_run:
        print("pdf downloader disabled in config; use dry-run for inspection")
        conn.close()
        return

    http = http_config(config)
    downloaded = 0
    blocked = 0
    for row in rows:
        allowed, robots_message = robots_allows(config, row["pdf_url"])
        target = local_pdf_path(config, row["paper_id"])
        if args.dry_run:
            print(f"{row['paper_id']} {row['pdf_url']} -> {target} | {robots_message}")
            continue
        if not allowed:
            blocked += 1
            mark(conn, row["paper_id"], "blocked_by_policy", robots_message)
            if bool(pdf_cfg.get("stop_on_robots_block", True)):
                break
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            blob = fetch_bytes(row["pdf_url"], http, accept="application/pdf,*/*")
            digest = hashlib.sha256(blob).hexdigest()
            target.write_bytes(blob)
            mark(
                conn,
                row["paper_id"],
                "downloaded",
                "ok",
                local_path=str(target),
                sha256=digest,
                size_bytes=len(blob),
                downloaded_at=iso_z(utc_now()),
            )
            downloaded += 1
        except Exception as exc:
            mark(conn, row["paper_id"], "failed", str(exc))
            if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                break

    print(f"pdf candidates={len(rows)} downloaded={downloaded} blocked={blocked} mode={mode}")
    conn.close()


if __name__ == "__main__":
    main()
