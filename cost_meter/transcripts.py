"""Read Claude Code transcripts into sessions, turns (tasks) and API requests.

Layout, as observed on Claude Code transcripts in September 2026:

    <projects-dir>/<project-slug>/<session-id>.jsonl                       main thread
    <projects-dir>/<project-slug>/<session-id>/subagents/agent-<id>.jsonl  one sub-agent
    <projects-dir>/<project-slug>/<session-id>/subagents/agent-<id>.meta.json
                                                  {"toolUseId": <tool_use that spawned it>, ...}

One JSON object per line. What this module relies on:

- `assistant` lines carry `requestId`, `message.model`, `message.usage`, `timestamp` and one
  content block each: a request with N blocks is written on N lines that repeat the same
  usage (output_tokens may grow between them while streaming). A request is counted once.
- every `user` line of a turn carries the same `promptId`, the human prompt and the tool
  results alike; a new `promptId` starts a new turn.
- sub-agent lines live in their own file (`isSidechain: true`), not in the main file.
- `message.model == "<synthetic>"` marks a line Claude Code wrote itself (API error such as
  a 429), with zero usage: not an API request.
- the final usage of a request comes with its `stop_reason`. A request whose lines all carry
  `stop_reason: null` was written before that final usage: its output_tokens is a partial
  count (seen on sub-agent lines). It cannot be recovered; such requests are counted.
- a WebSearch tool result carries `toolUseResult.searchCount`: the searches billed for it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .pricing import TOKEN_FIELDS, empty_tokens

SYNTHETIC_MODEL = "<synthetic>"
SUBAGENTS_DIR = "subagents"
META_SUFFIX = ".meta.json"
NO_PROMPT = "(before first prompt)"
UNATTACHED = "(unattached sub-agents)"


class TranscriptError(Exception):
    """Transcripts cannot be read as asked (missing directory, unknown project)."""


def default_projects_dir() -> Path:
    """Where Claude Code writes transcripts: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "projects"


def project_slug(path: str) -> str:
    """Claude Code's directory name for a working directory: every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(os.path.expanduser(path)))


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Request:
    request_id: str
    model: str
    timestamp: Optional[datetime]
    end: Optional[datetime]
    tokens: Dict[str, int]
    speed: Optional[str]
    inference_geo: Optional[str]
    sidechain: bool
    source: str
    tool_use_ids: Set[str] = field(default_factory=set)
    has_text: bool = False
    aborted: bool = False
    cost: float = 0.0
    web_searches: Dict[str, int] = field(default_factory=dict)  # tool_use id -> searchCount
    stop_known: bool = False  # a line of this request has the stop_reason field
    stop_seen: bool = False   # a line of this request has a non-null stop_reason

    @property
    def web_search_count(self) -> int:
        return sum(self.web_searches.values())

    @property
    def incomplete_usage(self) -> bool:
        """Written before its final usage: output_tokens is under-recorded."""
        return self.stop_known and not self.stop_seen


@dataclass
class Turn:
    session_id: str
    project: str
    cwd: Optional[str]
    prompt_id: Optional[str]
    origin: str = "unknown"
    prompt: str = ""
    first_line: Optional[datetime] = None
    last_line: Optional[datetime] = None
    requests: List[Request] = field(default_factory=list)
    retries: int = 0
    api_errors: int = 0
    subagents: int = 0

    def touch(self, ts: Optional[datetime]) -> None:
        """Record the timestamp of a main-thread line that is not an API request."""
        if ts is None:
            return
        if self.first_line is None or ts < self.first_line:
            self.first_line = ts
        if self.last_line is None or ts > self.last_line:
            self.last_line = ts

    def _stamps(self) -> List[datetime]:
        stamps = [ts for ts in (self.first_line, self.last_line) if ts is not None]
        for r in self.main_requests:
            stamps += [ts for ts in (r.timestamp, r.end) if ts is not None]
        return stamps

    @property
    def start(self) -> Optional[datetime]:
        return min(self._stamps(), default=None)

    @property
    def end(self) -> Optional[datetime]:
        return max(self._stamps(), default=None)

    @property
    def duration_s(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return (self.end - self.start).total_seconds()

    @property
    def main_requests(self) -> List[Request]:
        return [r for r in self.requests if not r.sidechain]

    @property
    def tool_uses(self) -> int:
        return sum(len(r.tool_use_ids) for r in self.requests)

    @property
    def idle(self) -> bool:
        """Spent tokens on the main thread but produced neither a tool call nor final text."""
        main = self.main_requests
        return bool(main) and not any(r.tool_use_ids or r.has_text for r in main)


@dataclass
class Session:
    session_id: str
    project: str
    cwd: Optional[str]
    turns: List[Turn]


@dataclass
class ReadStats:
    bad_lines: int = 0
    synthetic_lines: int = 0
    duplicate_requests: int = 0
    unattached_subagents: int = 0
    incomplete_usage: int = 0
    files: int = 0


def _iter_lines(path: Path, stats: ReadStats) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                stats.bad_lines += 1  # e.g. a line still being written; reported, not hidden
                continue
            if isinstance(obj, dict):
                yield obj


def _usage_tokens(usage: dict) -> Dict[str, int]:
    tokens = empty_tokens()
    tokens["input"] = int(usage.get("input_tokens") or 0)
    tokens["cache_read"] = int(usage.get("cache_read_input_tokens") or 0)
    tokens["output"] = int(usage.get("output_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        tokens["cache_write_5m"] = int(split.get("ephemeral_5m_input_tokens") or 0)
        tokens["cache_write_1h"] = int(split.get("ephemeral_1h_input_tokens") or 0)
        # Any write the split does not account for is priced at the default (5-minute) TTL.
        tokens["cache_write_5m"] += max(0, written - tokens["cache_write_5m"] - tokens["cache_write_1h"])
    else:
        tokens["cache_write_5m"] = written
    return tokens


def _content_blocks(obj: dict) -> list:
    content = (obj.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def _human_text(obj: dict) -> str:
    if obj.get("isMeta"):
        return ""
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    else:
        text = " ".join(
            b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"
        )
    return " ".join(text.split())


def _origin(obj: dict) -> Optional[str]:
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind"):
        return str(origin["kind"])
    if obj.get("promptSource"):
        return str(obj["promptSource"])
    return None


class _SessionReader:
    """Reads one session: its main file, then its sub-agent files."""

    def __init__(self, main_path: Path, project: str, stats: ReadStats):
        self.main_path = main_path
        self.project = project
        self.stats = stats
        self.session_id = main_path.stem
        self.cwd: Optional[str] = None
        self.turns: List[Turn] = []
        self.by_prompt: Dict[str, Turn] = {}
        self.special: Dict[str, Turn] = {}
        self.requests: Dict[str, Tuple[Request, Turn]] = {}
        self.tool_use_turn: Dict[str, Turn] = {}
        self.tool_use_request: Dict[str, Request] = {}

    def _turn(self, prompt_id: Optional[str]) -> Turn:
        turn = Turn(self.session_id, self.project, self.cwd, prompt_id)
        self.turns.append(turn)
        return turn

    def _special(self, label: str) -> Turn:
        if label not in self.special:
            turn = self._turn(None)
            turn.origin = label
            self.special[label] = turn
        return self.special[label]

    def _add_assistant(self, obj: dict, turn: Turn, source: str, sidechain: bool) -> None:
        message = obj.get("message") or {}
        model = message.get("model") or ""
        ts = parse_ts(obj.get("timestamp"))
        if model == SYNTHETIC_MODEL:
            self.stats.synthetic_lines += 1
            turn.api_errors += 1
            if not sidechain:
                turn.touch(ts)
            return
        usage = message.get("usage") or {}
        rid = obj.get("requestId") or message.get("id") or obj.get("uuid")
        tokens = _usage_tokens(usage)
        existing = self.requests.get(rid)
        if existing is None:
            req = Request(rid, model, ts, ts, tokens, usage.get("speed"), usage.get("inference_geo"),
                          sidechain, source)
            self.requests[rid] = (req, turn)
            turn.requests.append(req)
        else:
            req, owner = existing
            if req.source != source:
                # Same request copied into another file (continued session, forked agent).
                self.stats.duplicate_requests += 1
                return
            for k in TOKEN_FIELDS:  # repeated usage: keep the most complete value, never add
                req.tokens[k] = max(req.tokens[k], tokens[k])
            req.speed = req.speed or usage.get("speed")
            req.inference_geo = req.inference_geo or usage.get("inference_geo")
            if ts is not None:
                req.timestamp = min(req.timestamp or ts, ts)
                req.end = max(req.end or ts, ts)
            turn = owner
        if "stop_reason" in message:
            req.stop_known = True
            req.stop_seen = req.stop_seen or bool(message["stop_reason"])
        for block in _content_blocks(obj):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("id"):
                req.tool_use_ids.add(block["id"])
                self.tool_use_turn[block["id"]] = turn
                self.tool_use_request[block["id"]] = req
            elif block.get("type") == "text" and str(block.get("text", "")).strip():
                req.has_text = True
        if obj.get("isAbortedMidStream"):
            req.aborted = True

    @staticmethod
    def _count_errors(obj: dict, turn: Turn) -> None:
        for block in _content_blocks(obj):
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                turn.retries += 1

    def _count_searches(self, obj: dict) -> None:
        """Web searches billed for a WebSearch tool result, set (not added) on the request
        that called the tool, so a copied result line is not counted twice."""
        result = obj.get("toolUseResult")
        if not isinstance(result, dict) or not isinstance(result.get("searchCount"), int):
            return
        for block in _content_blocks(obj):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                req = self.tool_use_request.get(block.get("tool_use_id") or "")
                if req is not None:
                    req.web_searches[block["tool_use_id"]] = result["searchCount"]

    def read_main(self) -> None:
        current: Optional[Turn] = None
        source = str(self.main_path)
        for obj in _iter_lines(self.main_path, self.stats):
            kind = obj.get("type")
            if self.cwd is None and obj.get("cwd"):
                self.cwd = obj["cwd"]
                for t in self.turns:
                    t.cwd = self.cwd
            if kind == "user":
                pid = obj.get("promptId")
                if pid and (current is None or current.prompt_id != pid):
                    current = self.by_prompt.get(pid)
                    if current is None:
                        current = self._turn(pid)
                        self.by_prompt[pid] = current
                turn = current or self._special(NO_PROMPT)
                turn.touch(parse_ts(obj.get("timestamp")))
                if turn.origin == "unknown":
                    turn.origin = _origin(obj) or ("meta" if obj.get("isMeta") else "unknown")
                if not turn.prompt:
                    turn.prompt = _human_text(obj)
                self._count_errors(obj, turn)
                self._count_searches(obj)
            elif kind == "assistant":
                self._add_assistant(obj, current or self._special(NO_PROMPT), source, sidechain=False)

    def read_subagents(self) -> None:
        folder = self.main_path.with_suffix("") / SUBAGENTS_DIR
        if not folder.is_dir():
            return
        parsed = []
        for path in sorted(folder.glob("*.jsonl")):
            self.stats.files += 1
            meta_path = path.with_name(path.stem + META_SUFFIX)
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    self.stats.bad_lines += 1
            lines = list(_iter_lines(path, self.stats))
            parsed.append((path, meta, lines))
        # Nested sub-agents are spawned by a tool_use inside another sub-agent file:
        # resolve in rounds until no file can be attached any more.
        pending = parsed
        while pending:
            left = []
            for path, meta, lines in pending:
                turn = self.tool_use_turn.get(meta.get("toolUseId") or "")
                if turn is None:
                    pids = [o.get("promptId") for o in lines if o.get("promptId")]
                    turn = next((self.by_prompt[p] for p in pids if p in self.by_prompt), None)
                if turn is None:
                    left.append((path, meta, lines))
                    continue
                self._attach(path, lines, turn)
            if len(left) == len(pending):
                for path, _meta, lines in left:
                    self.stats.unattached_subagents += 1
                    self._attach(path, lines, self._special(UNATTACHED))
                break
            pending = left

    def _attach(self, path: Path, lines: List[dict], turn: Turn) -> None:
        turn.subagents += 1
        for obj in lines:
            if obj.get("type") == "assistant":
                self._add_assistant(obj, turn, str(path), sidechain=True)
            elif obj.get("type") == "user":
                self._count_errors(obj, turn)
                self._count_searches(obj)

    def session(self) -> Session:
        turns = [t for t in self.turns if t.requests or t.first_line is not None]
        return Session(self.session_id, self.project, self.cwd, turns)


def _latest_mtime(main_path: Path) -> float:
    latest = main_path.stat().st_mtime
    folder = main_path.with_suffix("") / SUBAGENTS_DIR
    if folder.is_dir():
        for p in folder.iterdir():
            latest = max(latest, p.stat().st_mtime)
    return latest


def find_sessions(projects_dir: Path, project: Optional[str] = None,
                  since: Optional[datetime] = None) -> List[Tuple[Path, str]]:
    """Main transcript files to read, as (path, project slug)."""
    if not projects_dir.is_dir():
        raise TranscriptError(f"transcripts directory not found: {projects_dir}")
    if project:
        slug = project_slug(project) if ("/" in project or project.startswith("~") or project == ".") else project
        dirs = [projects_dir / slug]
        if not dirs[0].is_dir():
            raise TranscriptError(f"no transcripts for project {project!r} (looked for {dirs[0]})")
    else:
        dirs = sorted(p for p in projects_dir.iterdir() if p.is_dir())
    found = []
    for d in dirs:
        for path in sorted(d.glob("*.jsonl")):
            # Nothing written since the window opened. A continued session copies earlier
            # lines with their original timestamps, so turns copied from a skipped file fall
            # before the window too.
            if since is not None and _latest_mtime(path) < since.timestamp():
                continue
            found.append((path, d.name))
    return found


def read_sessions(projects_dir: Path, project: Optional[str] = None,
                  session_ids: Optional[List[str]] = None,
                  since: Optional[datetime] = None) -> Tuple[List[Session], ReadStats]:
    """Read and de-duplicate sessions. With `session_ids`, the other sessions of the same
    project directories are still read, so that a continued session does not count the
    requests it copied from its predecessor; only the asked sessions are returned."""
    stats = ReadStats()
    sessions = []
    for path, slug in find_sessions(projects_dir, project, since):
        stats.files += 1
        reader = _SessionReader(path, slug, stats)
        reader.read_main()
        reader.read_subagents()
        sessions.append(reader.session())
    _dedupe_across_sessions(sessions, stats)
    stats.incomplete_usage = sum(r.incomplete_usage for s in sessions for t in s.turns for r in t.requests)
    if session_ids:
        wanted = set(session_ids)
        missing = wanted - {s.session_id for s in sessions}
        if missing:
            window = " with activity in the --since window" if since is not None else ""
            raise TranscriptError(f"session(s) not found{window}: {', '.join(sorted(missing))}")
        sessions = [s for s in sessions if s.session_id in wanted]
    return sessions, stats


def _dedupe_across_sessions(sessions: List[Session], stats: ReadStats) -> None:
    """A continued session re-writes earlier lines: the earliest session keeps the request."""
    far_future = datetime.max.replace(tzinfo=timezone.utc)

    def start(s: Session) -> datetime:
        return min((t.start for t in s.turns if t.start), default=far_future)

    seen: Set[str] = set()
    for s in sorted(sessions, key=start):
        remaining = []
        for t in s.turns:
            kept = []
            for r in t.requests:
                if r.request_id in seen:
                    stats.duplicate_requests += 1
                    continue
                seen.add(r.request_id)
                kept.append(r)
            if t.requests and not kept:
                continue  # a turn made only of copied requests is the original's copy
            t.requests = kept
            remaining.append(t)
        s.turns = remaining
