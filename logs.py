#!/usr/bin/env python3
"""
Athena Chat Log Viewer
Fetches and displays chat logs directly from tabAI Chat Log in MariaDB.

Usage:
    python3 logs.py                        # last 20 rows
    python3 logs.py -n 50                  # last 50 rows
    python3 logs.py -n 100 --user krupal.v@solarsquare.in
    python3 logs.py --session sess-abc123
    python3 logs.py --failed               # only errors / no-docs / incomplete
    python3 logs.py --stats                # summary stats only
    python3 logs.py --since 2026-04-01     # from a date
    python3 logs.py -n 10 --json           # raw JSON output
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# DB connection (reuse config from db/config.json)
# ---------------------------------------------------------------------------

def _get_connection():
    # When running inside the container (docker exec), use /db/config.json.
    # When running on the host, use db/config.json relative to this file.
    for candidate in [Path("/db/config.json"), Path(__file__).parent / "db" / "config.json"]:
        if candidate.exists():
            config_path = candidate
            break
    else:
        raise FileNotFoundError("db/config.json not found")

    with open(config_path) as f:
        cfg = json.load(f)

    import pymysql
    import pymysql.cursors

    # Inside Docker the host is the MariaDB container name.
    # On the host machine override via DB_HOST env var (e.g. 127.0.0.1 if port is exposed).
    host = os.environ.get("DB_HOST", cfg.get("db_host", "mariadb"))
    return pymysql.connect(
        host=host,
        port=int(cfg.get("db_port", 3306)),
        user=cfg.get("db_name", ""),
        password=cfg.get("db_password", ""),
        database=cfg.get("db_name", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


# ---------------------------------------------------------------------------
# Answer quality classifier
# ---------------------------------------------------------------------------

def _classify(answer: str) -> str:
    a = (answer or "").lower()
    if "temporarily unavailable" in a or "not responding" in a:
        return "LLM_TIMEOUT"
    if "unknown column" in a or "query failed" in a or "query rejected" in a or "syntax error" in a:
        return "SQL_ERROR"
    if "i don't have documentation" in a or "i don't know" in a:
        return "NO_DOCS"
    if "please specify" in a or "no records found" in a or "not found" in a or "no open tasks" in a:
        return "INCOMPLETE"
    if "could not generate" in a or "try rephrasing" in a:
        return "GEN_FAILED"
    return "OK"


_STATUS_COLOUR = {
    "OK":          "\033[92m",   # green
    "INCOMPLETE":  "\033[93m",   # yellow
    "NO_DOCS":     "\033[93m",   # yellow
    "SQL_ERROR":   "\033[91m",   # red
    "LLM_TIMEOUT": "\033[91m",   # red
    "GEN_FAILED":  "\033[91m",   # red
}
_RESET = "\033[0m"
_BOLD  = "\033[1m"
_FAILED_STATUSES = {"SQL_ERROR", "LLM_TIMEOUT", "NO_DOCS", "INCOMPLETE", "GEN_FAILED"}


def _coloured(text: str, status: str) -> str:
    col = _STATUS_COLOUR.get(status, "")
    return f"{col}{text}{_RESET}"


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_logs(
    n: int = 20,
    user: Optional[str] = None,
    session: Optional[str] = None,
    since: Optional[str] = None,
    failed_only: bool = False,
) -> list[dict]:
    conditions = []
    params = []

    if user:
        conditions.append("`user` = %s")
        params.append(user)
    if session:
        conditions.append("`session_id` = %s")
        params.append(session)
    if since:
        conditions.append("`creation` >= %s")
        params.append(since)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT name, user, question, answer, session_id,
               current_doctype, current_doc, creation
        FROM `tabAI Chat Log`
        {where}
        ORDER BY creation DESC
        LIMIT %s
    """
    params.append(n)

    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    rows = [dict(r) for r in rows]

    # Attach status classification
    for r in rows:
        r["_status"] = _classify(r.get("answer", ""))

    if failed_only:
        rows = [r for r in rows if r["_status"] in _FAILED_STATUSES]

    return rows


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(rows: list[dict]):
    from collections import Counter
    total = len(rows)
    statuses = Counter(r["_status"] for r in rows)
    users    = Counter(r.get("user") or "anonymous" for r in rows)

    print(f"\n{_BOLD}=== STATS ({total} rows) ==={_RESET}")
    print(f"\n  {'Status':<15} Count   %")
    print(f"  {'-'*30}")
    for status, count in sorted(statuses.items(), key=lambda x: -x[1]):
        pct = count / total * 100 if total else 0
        line = f"  {status:<15} {count:<7} {pct:.1f}%"
        print(_coloured(line, status))

    print(f"\n  {'User':<40} Count")
    print(f"  {'-'*50}")
    for user, count in users.most_common():
        print(f"  {user:<40} {count}")

    # Failed questions
    failed = [r for r in rows if r["_status"] in _FAILED_STATUSES]
    if failed:
        print(f"\n{_BOLD}  Failed / degraded questions:{_RESET}")
        for r in failed:
            q = (r.get("question") or "")[:80]
            print(_coloured(f"  [{r['_status']}] {q}", r["_status"]))
    print()


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------

def print_rows(rows: list[dict], show_answer_len: int = 200):
    if not rows:
        print("  No rows found.")
        return

    for i, r in enumerate(reversed(rows), 1):
        status  = r["_status"]
        ts      = str(r.get("creation", ""))[:19]
        user    = (r.get("user") or "anonymous")[:35]
        q       = (r.get("question") or "").strip()
        a       = (r.get("answer") or "").strip()
        session = r.get("session_id") or ""
        doc     = r.get("current_doc") or ""
        doctype = r.get("current_doctype") or ""

        sep = "─" * 72
        print(f"\n{sep}")
        print(f"{_BOLD}#{i:>3}  {ts}  {user}{_RESET}  {_coloured(status, status)}")
        if session:
            print(f"      session: {session}")
        if doctype or doc:
            print(f"      context: {doctype} / {doc}")
        print(f"\n  {_BOLD}Q:{_RESET} {q}")
        print(f"\n  {_BOLD}A:{_RESET} {a[:show_answer_len]}{'…' if len(a) > show_answer_len else ''}")

    print(f"\n{'─'*72}")
    print(f"  Showing {len(rows)} row(s)\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Athena chat log viewer — reads from tabAI Chat Log in MariaDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("-n", "--rows",    type=int, default=20,  help="Number of rows to fetch (default: 20)")
    parser.add_argument("--user",          type=str, default=None, help="Filter by user email")
    parser.add_argument("--session",       type=str, default=None, help="Filter by session_id")
    parser.add_argument("--since",         type=str, default=None, help="Filter from date (YYYY-MM-DD)")
    parser.add_argument("--failed",        action="store_true",    help="Show only failed / degraded responses")
    parser.add_argument("--stats",         action="store_true",    help="Show summary stats only (no full logs)")
    parser.add_argument("--json",          action="store_true",    help="Output raw JSON")
    parser.add_argument("--answer-len",    type=int, default=200,  help="Max answer chars to display (default: 200)")

    args = parser.parse_args()

    try:
        rows = fetch_logs(
            n=args.rows,
            user=args.user,
            session=args.session,
            since=args.since,
            failed_only=args.failed,
        )
    except FileNotFoundError:
        print("ERROR: db/config.json not found. Run from the chatbot root directory.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Could not connect to MariaDB: {e}")
        sys.exit(1)

    if args.json:
        # Strip internal _status key for clean JSON
        clean = [{k: v for k, v in r.items() if k != "_status"} for r in rows]
        print(json.dumps(clean, default=str, indent=2))
        return

    if args.stats:
        # Fetch more rows for stats to be meaningful
        if args.rows == 20:
            rows = fetch_logs(n=1000, user=args.user, session=args.session, since=args.since)
        print_stats(rows)
        return

    print_stats(rows)
    print_rows(rows, show_answer_len=args.answer_len)


if __name__ == "__main__":
    main()
