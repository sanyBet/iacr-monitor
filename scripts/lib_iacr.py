#!/usr/bin/env python3
"""Shared helpers for the IACR ePrint monitor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config.toml"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "atom": "http://www.w3.org/2005/Atom",
}


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str
    from_email: str
    timeout_seconds: float
    retries: int
    delay_seconds: tuple[float, float]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def load_config(path: str | os.PathLike[str] | None = None) -> dict:
    config_path = Path(path or os.environ.get("IACR_MONITOR_CONFIG", DEFAULT_CONFIG))
    if not config_path.exists():
        raise SystemExit(f"Missing config: {config_path}")
    with config_path.open("rb") as fh:
        config = tomllib.load(fh)
    config.setdefault("paths", {})
    config.setdefault("http", {})
    config.setdefault("oai", {})
    config.setdefault("rss", {})
    config.setdefault("daily", {})
    config.setdefault("pdf", {})
    return config


def resolve_path(config: dict, key: str, default: str) -> Path:
    value = config.get("paths", {}).get(key, default)
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path


def http_config(config: dict) -> HttpConfig:
    http = config.get("http", {})
    delay = http.get("delay_seconds", [1.0, 2.5])
    if isinstance(delay, (int, float)):
        delay_pair = (float(delay), float(delay))
    else:
        delay_pair = (float(delay[0]), float(delay[-1]))
    return HttpConfig(
        user_agent=http.get("user_agent", "iacr-monitor/0.1"),
        from_email=http.get("from_email", ""),
        timeout_seconds=float(http.get("timeout_seconds", 30)),
        retries=int(http.get("retries", 3)),
        delay_seconds=delay_pair,
    )


def ensure_dirs(config: dict) -> None:
    for key, default in [
        ("db", "data/iacr.db"),
        ("raw_oai", "data/raw/oai"),
        ("raw_rss", "data/raw/rss"),
        ("reports", "reports/daily"),
        ("pdfs", "data/pdfs"),
    ]:
        path = resolve_path(config, key, default)
        if key == "db":
            path.parent.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)


def polite_sleep(config: HttpConfig) -> None:
    lo, hi = config.delay_seconds
    if hi <= 0:
        return
    time.sleep(random.uniform(max(0.0, lo), max(lo, hi)))


def fetch_bytes(url: str, config: HttpConfig, *, accept: str = "*/*") -> bytes:
    headers = {
        "User-Agent": config.user_agent,
        "Accept": accept,
    }
    if config.from_email:
        headers["From"] = config.from_email

    last_error: Exception | None = None
    for attempt in range(config.retries + 1):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=config.timeout_seconds) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429}:
                raise RuntimeError(f"remote refused request with HTTP {exc.code}: {url}") from exc
            last_error = exc
        except urllib.error.URLError as exc:
            last_error = exc
        if attempt < config.retries:
            time.sleep(min(60, 2**attempt))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def connect_db(config: dict) -> sqlite3.Connection:
    db_path = resolve_path(config, "db", "data/iacr.db")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS papers (
          paper_id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          abstract TEXT NOT NULL,
          authors_json TEXT NOT NULL,
          categories_json TEXT NOT NULL,
          url TEXT NOT NULL,
          pdf_url TEXT NOT NULL,
          submitted_at TEXT,
          last_updated_at TEXT,
          rights TEXT,
          source_hash TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          updated_seen_at TEXT NOT NULL,
          deleted INTEGER NOT NULL DEFAULT 0
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts
        USING fts5(paper_id UNINDEXED, title, abstract, authors);

        CREATE TABLE IF NOT EXISTS pdf_assets (
          paper_id TEXT PRIMARY KEY,
          pdf_url TEXT NOT NULL,
          local_path TEXT,
          sha256 TEXT,
          size_bytes INTEGER,
          status TEXT NOT NULL DEFAULT 'pending',
          message TEXT,
          downloaded_at TEXT,
          FOREIGN KEY (paper_id) REFERENCES papers(paper_id)
        );

        CREATE TABLE IF NOT EXISTS harvest_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          cursor TEXT,
          records_seen INTEGER NOT NULL DEFAULT 0,
          records_changed INTEGER NOT NULL DEFAULT 0,
          message TEXT
        );

        CREATE TABLE IF NOT EXISTS notifications_sent (
          paper_id TEXT NOT NULL,
          run_date TEXT NOT NULL,
          kind TEXT NOT NULL,
          sent_at TEXT NOT NULL,
          PRIMARY KEY (paper_id, run_date, kind)
        );
        """
    )
    conn.commit()


def normalize_paper_id(value: str) -> str:
    value = value.strip()
    if value.startswith("oai:eprint.iacr.org:"):
        return value.rsplit(":", 1)[-1]
    if "eprint.iacr.org/" in value:
        return value.rstrip("/").rsplit("/", 2)[-2] + "/" + value.rstrip("/").rsplit("/", 1)[-1]
    return value


def paper_hash(fields: dict) -> str:
    stable = json.dumps(fields, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def text_of_all(parent: ET.Element, tag: str) -> list[str]:
    return [
        (item.text or "").strip()
        for item in parent.findall(tag, NS)
        if (item.text or "").strip()
    ]


def extract_oai_records(xml_bytes: bytes) -> tuple[list[dict], str | None]:
    root = ET.fromstring(xml_bytes)
    records = []
    for record in root.findall(".//oai:record", NS):
        header = record.find("oai:header", NS)
        if header is None:
            continue
        identifier = (header.findtext("oai:identifier", default="", namespaces=NS) or "").strip()
        datestamp = (header.findtext("oai:datestamp", default="", namespaces=NS) or "").strip()
        status = header.attrib.get("status", "")
        paper_id = normalize_paper_id(identifier)
        if status == "deleted":
            records.append({"paper_id": paper_id, "deleted": True, "last_updated_at": datestamp})
            continue

        dc = record.find(".//{http://www.openarchives.org/OAI/2.0/oai_dc/}dc")
        if dc is None:
            continue
        identifiers = text_of_all(dc, "dc:identifier")
        url = next((x for x in identifiers if x.startswith("http")), f"https://eprint.iacr.org/{paper_id}")
        dates = text_of_all(dc, "dc:date")
        title = " ".join(text_of_all(dc, "dc:title")).strip()
        abstract = "\n\n".join(text_of_all(dc, "dc:description")).strip()
        authors = text_of_all(dc, "dc:creator")
        categories = text_of_all(dc, "dc:subject")
        rights = " ".join(text_of_all(dc, "dc:rights")).strip()
        submitted_at = dates[0] if dates else None
        last_updated_at = datestamp or (dates[-1] if dates else None)
        paper_id = normalize_paper_id(url)
        fields = {
            "paper_id": paper_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "categories": categories,
            "url": url,
            "submitted_at": submitted_at,
            "last_updated_at": last_updated_at,
            "rights": rights,
        }
        fields["source_hash"] = paper_hash(fields)
        fields["pdf_url"] = f"{url}.pdf"
        fields["deleted"] = False
        records.append(fields)

    token_el = root.find(".//oai:resumptionToken", NS)
    token = None
    if token_el is not None and (token_el.text or "").strip():
        token = (token_el.text or "").strip()
    return records, token


def upsert_paper(conn: sqlite3.Connection, paper: dict, *, seen_at: str | None = None) -> str:
    seen = seen_at or iso_z(utc_now())
    paper_id = paper["paper_id"]
    existing = conn.execute(
        "SELECT source_hash, deleted FROM papers WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    if paper.get("deleted"):
        if existing:
            conn.execute(
                "UPDATE papers SET deleted = 1, last_updated_at = ?, updated_seen_at = ? WHERE paper_id = ?",
                (paper.get("last_updated_at"), seen, paper_id),
            )
            return "updated"
        return "same"

    authors_json = json.dumps(paper.get("authors", []), ensure_ascii=False)
    categories_json = json.dumps(paper.get("categories", []), ensure_ascii=False)
    action = "same"
    if existing is None:
        action = "new"
        first_seen = seen
    elif existing["source_hash"] != paper["source_hash"] or existing["deleted"]:
        action = "updated"
        first_seen = conn.execute(
            "SELECT first_seen_at FROM papers WHERE paper_id = ?", (paper_id,)
        ).fetchone()[0]
    else:
        return "same"

    conn.execute(
        """
        INSERT INTO papers (
          paper_id, title, abstract, authors_json, categories_json, url, pdf_url,
          submitted_at, last_updated_at, rights, source_hash, first_seen_at,
          updated_seen_at, deleted
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(paper_id) DO UPDATE SET
          title=excluded.title,
          abstract=excluded.abstract,
          authors_json=excluded.authors_json,
          categories_json=excluded.categories_json,
          url=excluded.url,
          pdf_url=excluded.pdf_url,
          submitted_at=excluded.submitted_at,
          last_updated_at=excluded.last_updated_at,
          rights=excluded.rights,
          source_hash=excluded.source_hash,
          updated_seen_at=excluded.updated_seen_at,
          deleted=0
        """,
        (
            paper_id,
            paper.get("title", ""),
            paper.get("abstract", ""),
            authors_json,
            categories_json,
            paper.get("url", f"https://eprint.iacr.org/{paper_id}"),
            paper.get("pdf_url", f"https://eprint.iacr.org/{paper_id}.pdf"),
            paper.get("submitted_at"),
            paper.get("last_updated_at"),
            paper.get("rights", ""),
            paper["source_hash"],
            first_seen,
            seen,
        ),
    )
    conn.execute("DELETE FROM paper_fts WHERE paper_id = ?", (paper_id,))
    conn.execute(
        "INSERT INTO paper_fts(paper_id, title, abstract, authors) VALUES (?, ?, ?, ?)",
        (paper_id, paper.get("title", ""), paper.get("abstract", ""), " ".join(paper.get("authors", []))),
    )
    conn.execute(
        """
        INSERT INTO pdf_assets(paper_id, pdf_url, status)
        VALUES (?, ?, 'pending')
        ON CONFLICT(paper_id) DO UPDATE SET pdf_url=excluded.pdf_url
        """,
        (paper_id, paper.get("pdf_url", f"https://eprint.iacr.org/{paper_id}.pdf")),
    )
    return action


def latest_datestamp(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT max(last_updated_at) AS dt FROM papers WHERE deleted = 0 AND last_updated_at IS NOT NULL"
    ).fetchone()
    return row["dt"] if row and row["dt"] else None


def shifted_from_date(conn: sqlite3.Connection, overlap_hours: int) -> str | None:
    latest = latest_datestamp(conn)
    dt = parse_dt(latest)
    if not dt:
        return None
    return iso_z(dt - timedelta(hours=overlap_hours))


def local_report_date(config: dict) -> str:
    tz_name = config.get("daily", {}).get("timezone", "Asia/Shanghai")
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def compact_abstract(text: str, max_chars: int = 450) -> str:
    flat = " ".join(text.split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip() + "..."


def authors_display(authors_json: str, max_authors: int = 6) -> str:
    authors = json.loads(authors_json or "[]")
    if len(authors) <= max_authors:
        return ", ".join(authors)
    return ", ".join(authors[:max_authors]) + f", et al. ({len(authors)} authors)"


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)
