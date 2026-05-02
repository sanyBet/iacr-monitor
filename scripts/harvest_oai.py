#!/usr/bin/env python3
"""Harvest IACR ePrint OAI-PMH metadata into SQLite."""

from __future__ import annotations

import argparse
import sqlite3
import urllib.parse
from datetime import timezone
from pathlib import Path

from lib_iacr import (
    connect_db,
    ensure_dirs,
    extract_oai_records,
    fetch_bytes,
    http_config,
    init_db,
    iso_z,
    load_config,
    polite_sleep,
    positive_int,
    resolve_path,
    shifted_from_date,
    upsert_paper,
    utc_now,
)


def build_oai_url(base_url: str, *, metadata_prefix: str, from_date: str | None, until: str | None) -> str:
    params = {
        "verb": "ListRecords",
        "metadataPrefix": metadata_prefix,
    }
    if from_date:
        params["from"] = from_date
    if until:
        params["until"] = until
    return f"{base_url}?{urllib.parse.urlencode(params)}"


def build_token_url(base_url: str, token: str) -> str:
    return f"{base_url}?{urllib.parse.urlencode({'verb': 'ListRecords', 'resumptionToken': token})}"


def save_raw(raw_dir: Path, xml_bytes: bytes, *, kind: str, page: int) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = raw_dir / f"{stamp}_{kind}_{page:05d}.xml"
    path.write_bytes(xml_bytes)
    return path


def start_run(conn: sqlite3.Connection, kind: str, cursor: str | None) -> int:
    now = iso_z(utc_now())
    cur = conn.execute(
        "INSERT INTO harvest_runs(kind, started_at, status, cursor) VALUES (?, ?, 'running', ?)",
        (kind, now, cursor),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    cursor: str | None,
    records_seen: int,
    records_changed: int,
    message: str = "",
) -> None:
    conn.execute(
        """
        UPDATE harvest_runs
        SET finished_at = ?, status = ?, cursor = ?, records_seen = ?,
            records_changed = ?, message = ?
        WHERE id = ?
        """,
        (iso_z(utc_now()), status, cursor, records_seen, records_changed, message, run_id),
    )
    conn.commit()


def run_harvest(
    *,
    config: dict,
    mode: str,
    from_date: str | None = None,
    until: str | None = None,
    limit_pages: int | None = None,
    save_raw_pages: bool = True,
) -> dict:
    ensure_dirs(config)
    http = http_config(config)
    oai = config.get("oai", {})
    base_url = oai.get("base_url", "https://eprint.iacr.org/oai")
    metadata_prefix = oai.get("metadata_prefix", "oai_dc")
    overlap_hours = int(oai.get("incremental_overlap_hours", 48))
    raw_dir = resolve_path(config, "raw_oai", "data/raw/oai")

    conn = connect_db(config)
    init_db(conn)
    if mode == "incremental" and not from_date:
        from_date = shifted_from_date(conn, overlap_hours)
    run_id = start_run(conn, mode, from_date)

    page = 0
    token = None
    records_seen = 0
    records_changed = 0
    changes: list[dict] = []
    url = build_oai_url(base_url, metadata_prefix=metadata_prefix, from_date=from_date, until=until)

    try:
        while url:
            page += 1
            xml_bytes = fetch_bytes(url, http, accept="application/xml,text/xml,*/*")
            if save_raw_pages:
                save_raw(raw_dir, xml_bytes, kind=mode, page=page)
            records, token = extract_oai_records(xml_bytes)
            for paper in records:
                action = upsert_paper(conn, paper)
                records_seen += 1
                if action in {"new", "updated"}:
                    records_changed += 1
                    changes.append({"action": action, "paper_id": paper["paper_id"]})
            conn.commit()
            if limit_pages and page >= limit_pages:
                break
            if token:
                url = build_token_url(base_url, token)
                polite_sleep(http)
            else:
                url = ""
        finish_run(
            conn,
            run_id,
            status="success",
            cursor=token,
            records_seen=records_seen,
            records_changed=records_changed,
        )
    except Exception as exc:
        finish_run(
            conn,
            run_id,
            status="failed",
            cursor=token,
            records_seen=records_seen,
            records_changed=records_changed,
            message=str(exc),
        )
        conn.close()
        raise

    total = conn.execute("SELECT count(*) AS n FROM papers WHERE deleted = 0").fetchone()["n"]
    conn.close()
    return {
        "run_id": run_id,
        "mode": mode,
        "from_date": from_date,
        "pages": page,
        "records_seen": records_seen,
        "records_changed": records_changed,
        "total_papers": total,
        "changes": changes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest IACR OAI-PMH metadata")
    parser.add_argument("--mode", choices=["full", "incremental"], default="incremental")
    parser.add_argument("--from-date", help="OAI from datestamp, e.g. 2026-05-01T00:00:00Z")
    parser.add_argument("--until", help="OAI until datestamp")
    parser.add_argument("--limit-pages", type=positive_int, help="test mode page limit")
    parser.add_argument("--no-raw", action="store_true", help="do not persist raw OAI XML pages")
    args = parser.parse_args()

    config = load_config()
    result = run_harvest(
        config=config,
        mode=args.mode,
        from_date=args.from_date,
        until=args.until,
        limit_pages=args.limit_pages,
        save_raw_pages=not args.no_raw,
    )
    print(
        "harvest complete: "
        f"run_id={result['run_id']} mode={result['mode']} pages={result['pages']} "
        f"seen={result['records_seen']} changed={result['records_changed']} "
        f"total={result['total_papers']} from={result['from_date']}"
    )


if __name__ == "__main__":
    main()
