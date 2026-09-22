"""cost-meter command line."""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from . import __version__
from .pricing import PricingError, load_prices
from .report import GROUPINGS, group, price_turns, render_table, select_turns, to_csv, to_json, totals
from .transcripts import TranscriptError, default_projects_dir, read_sessions

STDOUT = "-"


def parse_since(value: str, now: Optional[datetime] = None) -> datetime:
    """'7d' (N days back from now) or 'YYYY-MM-DD' (local midnight)."""
    now = now or datetime.now().astimezone()
    match = re.fullmatch(r"(\d+)d", value)
    if match:
        return now - timedelta(days=int(match.group(1)))
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"--since expects Nd or YYYY-MM-DD, got {value!r}") from None
    return day.astimezone()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cost-meter",
        description="Cost, tokens, retries and waste per task, read from Claude Code transcripts.",
    )
    p.add_argument("--since", type=parse_since, metavar="7d|YYYY-MM-DD",
                   help="only turns that started at or after this point")
    p.add_argument("--project", metavar="SLUG|PATH",
                   help="one project: its transcripts directory name, or its working directory path")
    p.add_argument("--session", action="append", metavar="ID", dest="sessions",
                   help="only this session id (repeatable)")
    p.add_argument("--by", choices=GROUPINGS, default="session", help="grouping of the table (default: session)")
    p.add_argument("--json", metavar="OUT.json", help="write the full result as JSON ('-' for stdout)")
    p.add_argument("--csv", metavar="OUT.csv", help="write the grouped rows as CSV ('-' for stdout)")
    p.add_argument("--prices", metavar="PRICES.json", help="prices file (default: prices.json of this repository)")
    p.add_argument("--projects-dir", metavar="DIR", type=Path,
                   help="transcripts root (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)")
    p.add_argument("--version", action="version", version=f"cost-meter {__version__}")
    return p


def _write(target: str, text: str) -> None:
    if target == STDOUT:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    else:
        Path(target).write_text(text, encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        prices = load_prices(args.prices)
        sessions, stats = read_sessions(args.projects_dir or default_projects_dir(), args.project,
                                        args.sessions, args.since)
        turns = select_turns(sessions, args.since)
        price_turns(turns, prices)
    except (PricingError, TranscriptError) as exc:
        print(f"cost-meter: error: {exc}", file=sys.stderr)
        return 2
    rows = group(turns, args.by)
    total = totals(turns)
    filters = {
        "since": args.since.isoformat() if args.since else None,
        "project": args.project,
        "sessions": args.sessions,
    }
    if args.json:
        _write(args.json, to_json(rows, args.by, total, turns, prices, stats, filters))
    if args.csv:
        _write(args.csv, to_csv(rows, args.by))
    if STDOUT not in (args.json, args.csv):
        print(render_table(rows, args.by, total, prices, stats))
    return 0
