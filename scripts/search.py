#!/usr/bin/env python3
"""Search local IACR ePrint title/author/abstract index."""

from __future__ import annotations

import argparse

from lib_iacr import authors_display, compact_abstract, connect_db, init_db, load_config, positive_int


def main() -> None:
    parser = argparse.ArgumentParser(description="Search indexed IACR ePrint metadata")
    parser.add_argument("query", help="SQLite FTS5 query, e.g. zkSNARK OR lattice")
    parser.add_argument("--limit", type=positive_int, default=10)
    args = parser.parse_args()

    config = load_config()
    conn = connect_db(config)
    init_db(conn)
    rows = conn.execute(
        """
        SELECT p.paper_id, p.title, p.abstract, p.authors_json, p.url, p.last_updated_at,
               bm25(paper_fts) AS rank
        FROM paper_fts
        JOIN papers p ON p.paper_id = paper_fts.paper_id
        WHERE paper_fts MATCH ? AND p.deleted = 0
        ORDER BY rank
        LIMIT ?
        """,
        (args.query, args.limit),
    ).fetchall()
    for row in rows:
        print(f"{row['paper_id']} | {row['title']}")
        print(f"  authors: {authors_display(row['authors_json'])}")
        print(f"  updated: {row['last_updated_at'] or ''}")
        print(f"  url: {row['url']}")
        print(f"  abstract: {compact_abstract(row['abstract'], 260)}")
        print()
    print(f"results={len(rows)}")
    conn.close()


if __name__ == "__main__":
    main()
