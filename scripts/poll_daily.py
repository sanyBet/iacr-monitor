#!/usr/bin/env python3
"""Run daily IACR monitor, then write a Markdown digest."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import timezone
from pathlib import Path

from harvest_oai import run_harvest
from lib_iacr import (
    authors_display,
    compact_abstract,
    connect_db,
    ensure_dirs,
    fetch_bytes,
    http_config,
    init_db,
    iso_z,
    load_config,
    local_report_date,
    positive_int,
    resolve_path,
    utc_now,
)


def save_rss_snapshot(config: dict) -> Path:
    rss_url = config.get("rss", {}).get("url", "https://eprint.iacr.org/rss/rss.xml?order=recent")
    raw_dir = resolve_path(config, "raw_rss", "data/raw/rss")
    raw_dir.mkdir(parents=True, exist_ok=True)
    xml_bytes = fetch_bytes(rss_url, http_config(config), accept="application/rss+xml,application/atom+xml,text/xml,*/*")
    stamp = utc_now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"{stamp}_feed.xml"
    path.write_bytes(xml_bytes)
    return path


def load_changed_rows(conn: sqlite3.Connection, changes: list[dict]) -> list[sqlite3.Row]:
    if not changes:
        return []
    order = {change["paper_id"]: index for index, change in enumerate(changes)}
    action = {change["paper_id"]: change["action"] for change in changes}
    placeholders = ",".join("?" for _ in order)
    rows = conn.execute(
        f"""
        SELECT paper_id, title, abstract, authors_json, categories_json, url, pdf_url,
               submitted_at, last_updated_at, rights
        FROM papers
        WHERE paper_id IN ({placeholders})
        """,
        tuple(order.keys()),
    ).fetchall()
    rows = sorted(rows, key=lambda row: order[row["paper_id"]])
    for row in rows:
        row_action = action[row["paper_id"]]
        dict(row)["action"] = row_action
    return rows


def row_categories(row: sqlite3.Row) -> list[str]:
    try:
        return json.loads(row["categories_json"] or "[]")
    except json.JSONDecodeError:
        return []


def render_digest(config: dict, result: dict, rows: list[sqlite3.Row]) -> str:
    report_date = local_report_date(config)
    action_by_id = {item["paper_id"]: item["action"] for item in result.get("changes", [])}
    category_counter: Counter[str] = Counter()
    for row in rows:
        category_counter.update(row_categories(row))
    new_count = sum(1 for item in result.get("changes", []) if item["action"] == "new")
    updated_count = sum(1 for item in result.get("changes", []) if item["action"] == "updated")

    lines = [
        f"# IACR ePrint Daily Digest - {report_date}",
        "",
        f"- Generated: {iso_z(utc_now())}",
        f"- OAI pages: {result['pages']}",
        f"- Records seen: {result['records_seen']}",
        f"- New papers: {new_count}",
        f"- Updated papers: {updated_count}",
        f"- Total indexed papers: {result['total_papers']}",
        "",
    ]
    if category_counter:
        lines.append("## Categories")
        lines.append("")
        for category, count in category_counter.most_common():
            lines.append(f"- {category}: {count}")
        lines.append("")

    lines.append("## Papers")
    lines.append("")
    if not rows:
        lines.append("No new or updated papers found.")
        lines.append("")
    for row in rows:
        action = action_by_id.get(row["paper_id"], "updated")
        categories = ", ".join(row_categories(row)) or "uncategorized"
        lines.extend(
            [
                f"### [{row['paper_id']}] {row['title']}",
                "",
                f"- Status: {action}",
                f"- Authors: {authors_display(row['authors_json'])}",
                f"- Categories: {categories}",
                f"- Updated: {row['last_updated_at'] or ''}",
                f"- URL: {row['url']}",
                f"- PDF: {row['pdf_url']}",
                "",
                compact_abstract(row["abstract"]),
                "",
            ]
        )
    return "\n".join(lines)


def write_digest(config: dict, content: str) -> Path:
    reports = resolve_path(config, "reports", "reports/daily")
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{local_report_date(config)}.md"
    path.write_text(content, encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll IACR once and write daily digest")
    parser.add_argument("--from-date", help="override OAI from datestamp")
    parser.add_argument("--limit-pages", type=positive_int, help="test mode page limit")
    parser.add_argument("--skip-rss", action="store_true", help="skip RSS snapshot")
    args = parser.parse_args()

    config = load_config()
    ensure_dirs(config)
    if not args.skip_rss:
        rss_path = save_rss_snapshot(config)
        print(f"rss snapshot: {rss_path}")

    result = run_harvest(
        config=config,
        mode="incremental",
        from_date=args.from_date,
        limit_pages=args.limit_pages,
    )

    conn = connect_db(config)
    init_db(conn)
    rows = load_changed_rows(conn, result["changes"])
    digest = render_digest(config, result, rows)
    report_path = write_digest(config, digest)
    conn.close()
    print(
        f"digest: {report_path} pages={result['pages']} seen={result['records_seen']} "
        f"changed={result['records_changed']} total={result['total_papers']}"
    )


if __name__ == "__main__":
    main()
