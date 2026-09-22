"""Aggregate priced turns and render them as a text table, JSON or CSV."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, OrderedDict
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from .pricing import TOKEN_FIELDS, Prices, add_tokens, empty_tokens, total_tokens
from .transcripts import ReadStats, Session, Turn

GROUPINGS = ("turn", "session", "model", "project", "day")
PROMPT_PREVIEW_CHARS = 80


def price_turns(turns: List[Turn], prices: Prices) -> None:
    """Price every request of the selected turns. Checks all models first, so the error
    names every unknown one at once."""
    counts = Counter(r.model for t in turns for r in t.requests)
    prices.check_models(counts)
    for t in turns:
        for r in t.requests:
            r.cost = prices.cost(r.model, r.tokens, r.speed, r.inference_geo, r.web_search_count)


def select_turns(sessions: List[Session], since: Optional[datetime]) -> List[Turn]:
    turns = [t for s in sessions for t in s.turns]
    if since is not None:
        turns = [t for t in turns if t.start is not None and t.start >= since]
    return sorted(turns, key=lambda t: (t.start is None, t.start or datetime.min, t.session_id))


def waste(turn: Turn) -> Dict[str, float]:
    """Waste = requests aborted mid-stream, plus the whole cost of an idle turn."""
    wasted = turn.requests if turn.idle else [r for r in turn.requests if r.aborted]
    return {
        "usd": sum(r.cost for r in wasted),
        "tokens": sum(total_tokens(r.tokens) for r in wasted),
    }


def _local_day(ts: Optional[datetime]) -> str:
    return ts.astimezone().date().isoformat() if ts else "unknown"


def _row(key: str) -> dict:
    return {"key": key, "turns": 0, "requests": 0, "tokens": empty_tokens(), "cost_usd": 0.0,
            "retries": 0, "waste_usd": 0.0, "waste_tokens": 0, "duration_s": 0.0}


def _add_turn(row: dict, turn: Turn) -> None:
    row["turns"] += 1
    row["requests"] += len(turn.requests)
    for r in turn.requests:
        add_tokens(row["tokens"], r.tokens)
    row["cost_usd"] += sum(r.cost for r in turn.requests)
    row["retries"] += turn.retries
    w = waste(turn)
    row["waste_usd"] += w["usd"]
    row["waste_tokens"] += w["tokens"]
    row["duration_s"] += turn.duration_s


def turn_record(turn: Turn) -> dict:
    w = waste(turn)
    tokens = empty_tokens()
    for r in turn.requests:
        add_tokens(tokens, r.tokens)
    return {
        "session_id": turn.session_id,
        "project": turn.project,
        "cwd": turn.cwd,
        "prompt_id": turn.prompt_id,
        "origin": turn.origin,
        "prompt": turn.prompt[:PROMPT_PREVIEW_CHARS],
        "start": turn.start.isoformat() if turn.start else None,
        "end": turn.end.isoformat() if turn.end else None,
        "duration_s": turn.duration_s,
        "requests": len(turn.requests),
        "subagent_requests": len(turn.requests) - len(turn.main_requests),
        "subagents": turn.subagents,
        "tool_uses": turn.tool_uses,
        "web_searches": sum(r.web_search_count for r in turn.requests),
        "models": sorted({r.model for r in turn.requests}),
        "tokens": tokens,
        "cost_usd": sum(r.cost for r in turn.requests),
        "retries": turn.retries,
        "aborted_requests": sum(1 for r in turn.requests if r.aborted),
        "idle": turn.idle,
        "api_errors": turn.api_errors,
        "waste_usd": w["usd"],
        "waste_tokens": w["tokens"],
    }


def group(turns: List[Turn], by: str) -> List[dict]:
    if by == "turn":
        return [turn_record(t) for t in turns]
    rows: "OrderedDict[str, dict]" = OrderedDict()
    if by == "model":
        # A turn can use several models: tokens and cost are split by request.
        for t in turns:
            wasted = {id(r) for r in (t.requests if t.idle else [r for r in t.requests if r.aborted])}
            for model in sorted({r.model for r in t.requests}):
                row = rows.setdefault(model, _row(model))
                row["turns"] += 1
                for r in t.requests:
                    if r.model != model:
                        continue
                    row["requests"] += 1
                    add_tokens(row["tokens"], r.tokens)
                    row["cost_usd"] += r.cost
                    if id(r) in wasted:
                        row["waste_usd"] += r.cost
                        row["waste_tokens"] += total_tokens(r.tokens)
            # retries and duration belong to a turn, not to a model: not reported here
        for row in rows.values():
            row["retries"] = None
            row["duration_s"] = None
        return list(rows.values())
    for t in turns:
        if by == "session":
            key = t.session_id
        elif by == "project":
            key = t.project
        elif by == "day":
            key = _local_day(t.start)
        else:
            raise ValueError(f"unknown grouping {by!r}")
        row = rows.setdefault(key, _row(key))
        if by == "session":
            row["project"] = t.project
            row["cwd"] = t.cwd
        _add_turn(row, t)
    return list(rows.values())


def totals(turns: List[Turn]) -> dict:
    row = _row("total")
    for t in turns:
        _add_turn(row, t)
    row["sessions"] = len({t.session_id for t in turns})
    return row


# ---------------------------------------------------------------- rendering

def _compact(n: Optional[float]) -> str:
    if n is None:
        return "-"
    n = float(n)
    for size, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(n) >= size:
            return f"{n / size:.1f}{suffix}"
    return f"{n:.0f}"


def _duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    minutes, s = divmod(int(round(seconds)), 60)
    h, m = divmod(minutes, 60)
    return f"{h}h{m:02d}" if h else f"{m}m{s:02d}"


def _usd(value: float) -> str:
    return f"{value:.2f}"


def _cache_write(tokens: Dict[str, int]) -> int:
    return tokens["cache_write_5m"] + tokens["cache_write_1h"]


def _table(headers: List[str], rows: Iterable[List[str]], left: int = 1) -> str:
    rows = list(rows)
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    lines = []
    for cells in [headers] + rows:
        out = []
        for i, c in enumerate(cells):
            out.append(c.ljust(widths[i]) if i < left or i == len(cells) - 1 and headers[-1] == "prompt"
                       else c.rjust(widths[i]))
        lines.append("  ".join(out).rstrip())
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def render_table(rows: List[dict], by: str, total: dict, prices: Prices, stats: ReadStats) -> str:
    if by == "turn":
        headers = ["session", "start", "origin", "req", "input", "cache_w", "cache_r", "output",
                   "cost_usd", "retries", "waste_usd", "duration", "prompt"]
        body = [[
            r["session_id"][:8],
            (datetime.fromisoformat(r["start"]).astimezone().strftime("%m-%d %H:%M") if r["start"] else "-"),
            r["origin"], str(r["requests"]), _compact(r["tokens"]["input"]),
            _compact(_cache_write(r["tokens"])), _compact(r["tokens"]["cache_read"]),
            _compact(r["tokens"]["output"]), _usd(r["cost_usd"]), str(r["retries"]),
            _usd(r["waste_usd"]), _duration(r["duration_s"]), r["prompt"],
        ] for r in rows]
    else:
        first = {"session": "session", "model": "model", "project": "project", "day": "day"}[by]
        headers = [first] + (["project"] if by == "session" else []) + [
            "turns", "req", "input", "cache_w", "cache_r", "output", "cost_usd", "retries",
            "waste_usd", "duration"]
        body = []
        for r in rows:
            key = r["key"][:8] if by == "session" else r["key"]
            extra = [(r.get("cwd") or r["project"]).rstrip("/").split("/")[-1]] if by == "session" else []
            body.append([key] + extra + [
                str(r["turns"]), str(r["requests"]), _compact(r["tokens"]["input"]),
                _compact(_cache_write(r["tokens"])), _compact(r["tokens"]["cache_read"]),
                _compact(r["tokens"]["output"]), _usd(r["cost_usd"]),
                "-" if r["retries"] is None else str(r["retries"]), _usd(r["waste_usd"]),
                _duration(r["duration_s"])])
    out = [_table(headers, body, left=3 if by == "turn" else (2 if by == "session" else 1))]
    out.append("")
    out.append(
        f"Total: {total['sessions']} sessions, {total['turns']} turns, {total['requests']} requests, "
        f"cost {_usd(total['cost_usd'])} {prices.currency}, retries {total['retries']}, "
        f"waste {_usd(total['waste_usd'])} {prices.currency}, "
        f"tokens in {_compact(total['tokens']['input'])} / cache write "
        f"{_compact(_cache_write(total['tokens']))} / cache read {_compact(total['tokens']['cache_read'])} "
        f"/ out {_compact(total['tokens']['output'])}"
    )
    out.append(f"Prices: {prices.path} (retrieved {prices.retrieved}, {prices.sources.get('prices', '')}). "
               "API-equivalent cost, not what a subscription bills.")
    notes = _stats_notes(stats)
    if notes:
        out.append("Notes: " + "; ".join(notes) + ".")
    return "\n".join(out)


def _stats_notes(stats: ReadStats) -> List[str]:
    notes = []
    if stats.bad_lines:
        notes.append(f"{stats.bad_lines} unreadable lines skipped")
    if stats.duplicate_requests:
        notes.append(f"{stats.duplicate_requests} requests copied across files counted once")
    if stats.synthetic_lines:
        notes.append(f"{stats.synthetic_lines} '<synthetic>' API-error lines (no tokens) not counted as requests")
    if stats.incomplete_usage:
        notes.append(f"{stats.incomplete_usage} requests written without their final usage "
                     "(output tokens under-recorded)")
    if stats.unattached_subagents:
        notes.append(f"{stats.unattached_subagents} sub-agent files not linked to a turn")
    return notes


def to_json(rows: List[dict], by: str, total: dict, turns: List[Turn], prices: Prices,
            stats: ReadStats, filters: dict) -> str:
    doc = {
        "tool": "agent-cost-meter",
        "generated_at": datetime.now().astimezone().isoformat(),
        "filters": filters,
        "currency": prices.currency,
        "prices": {"file": prices.path, "retrieved": prices.retrieved, "sources": prices.sources},
        "token_fields": list(TOKEN_FIELDS),
        "totals": total,
        "by": by,
        "rows": rows,
        "turns": rows if by == "turn" else [turn_record(t) for t in turns],
        "read_stats": vars(stats),
    }
    return json.dumps(doc, indent=2, ensure_ascii=False)


def to_csv(rows: List[dict], by: str) -> str:
    buf = io.StringIO()
    if by == "turn":
        fields = ["session_id", "project", "prompt_id", "origin", "start", "end", "duration_s", "requests",
                  "subagents", "tool_uses", *TOKEN_FIELDS, "cost_usd", "retries", "aborted_requests",
                  "idle", "waste_usd", "waste_tokens", "prompt"]
    else:
        fields = ["key", "turns", "requests", *TOKEN_FIELDS, "cost_usd", "retries", "waste_usd",
                  "waste_tokens", "duration_s"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        flat = dict(r)
        flat.update(r["tokens"])
        writer.writerow(flat)
    return buf.getvalue()
