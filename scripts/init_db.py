#!/usr/bin/env python3
from lib_iacr import connect_db, ensure_dirs, init_db, load_config, resolve_path


def main() -> None:
    config = load_config()
    ensure_dirs(config)
    conn = connect_db(config)
    init_db(conn)
    conn.close()
    print(f"initialized {resolve_path(config, 'db', 'data/iacr.db')}")


if __name__ == "__main__":
    main()
