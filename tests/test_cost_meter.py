"""Tests on synthetic fixtures only (tests/fixtures): no real transcript is used or committed.

Expected costs are computed by hand from tests/fixtures/prices.json (USD per million tokens):
claude-test-large: input 1, output 10; claude-test-small: input 0.5, output 2, cache read 0.2x;
cache writes 1.25x (5 min) and 2x (1 h), cache read 0.1x of the input price;
web search 20 USD per 1,000 searches.
"""

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cost_meter.cli import main, parse_since
from cost_meter.pricing import PricingError, UnknownModelError, load_prices
from cost_meter.report import group, price_turns, select_turns, totals, turn_record, waste
from cost_meter.transcripts import TranscriptError, project_slug, read_sessions

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TEST_PRICES = str(FIXTURES / "prices.json")
DEMO = "-tmp-demo-project"
OTHER = "-tmp-other-project"
SINCE = datetime(2026, 1, 8, tzinfo=timezone.utc)
M = 1e-6  # one token at 1 USD per million tokens

# Hand-computed costs, one per request.
REQ_A = (100 * 1 + 1000 * 1.25 + 2000 * 2 + 10000 * 0.1 + 40 * 10) * M  # output kept at 40, not 10 or 50
REQ_B = (10 + 20000 * 0.1 + 100 * 10) * M
REQ_C = (5 + 21000 * 0.1 + 50 * 10) * M
REQ_S1 = (200 * 0.5 + 4000 * 0.5 * 1.25 + 30 * 2) * M
REQ_S2 = (10 * 0.5 + 4000 * 0.5 * 0.2 + 20 * 2) * M
REQ_D = (20 + 21000 * 0.1 + 7 * 10) * M
REQ_D2 = (5 + 21000 * 0.1 + 60 * 10) * M
REQ_E = (5 + 22000 * 0.1 + 30 * 10) * M
REQ_O = (1000 + 1000 * 10) * M
REQ_X = (2000 * 0.5 + 500 * 2) * M
SEARCH = 20 / 1000  # one web search at the fixture price
REQ_W1 = (10 * 1 + 20 * 10) * M + 2 * SEARCH  # final usage (output 20), plus 2 searches
REQ_W2 = (5 + 1000 * 0.1 + 10 * 10) * M
REQ_T1 = (100 * 0.5 + 1 * 2) * M + 1 * SEARCH  # output stuck at the partial count: 1
REQ_T2 = (20 * 0.5 + 8 * 2) * M
TURN_1 = REQ_A + REQ_B + REQ_C + REQ_S1 + REQ_S2
TURN_2 = REQ_D + REQ_D2
TURN_3 = REQ_E


class FixtureDir:
    """A temporary Claude Code projects directory holding the synthetic transcripts."""

    def __enter__(self) -> Path:
        self.tmp = tempfile.mkdtemp(prefix="cost-meter-test-")
        root = Path(self.tmp)
        (root / DEMO).mkdir()
        (root / OTHER).mkdir()
        shutil.copy(FIXTURES / "session.jsonl", root / DEMO / "session.jsonl")
        shutil.copytree(FIXTURES / "session", root / DEMO / "session")
        shutil.copy(FIXTURES / "continued-session.jsonl", root / DEMO / "continued-session.jsonl")
        shutil.copy(FIXTURES / "elsewhere-session.jsonl", root / OTHER / "elsewhere-session.jsonl")
        return root

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp)


def load(root, since=SINCE, project=None, sessions=None):
    all_sessions, stats = read_sessions(root, project, sessions, since)
    turns = select_turns(all_sessions, since)
    price_turns(turns, load_prices(TEST_PRICES))
    return turns, stats


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ParsingTests(unittest.TestCase):
    def setUp(self):
        self.ctx = FixtureDir()
        self.root = self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__()

    def turn(self, turns, prompt_id, session="session"):
        return next(t for t in turns if t.prompt_id == prompt_id and t.session_id == session)

    def test_repeated_usage_is_counted_once_per_request(self):
        turns, _ = load(self.root, project=DEMO)
        t1 = self.turn(turns, "p1")
        req_a = [r for r in t1.requests if r.request_id == "req_A"]
        self.assertEqual(len(req_a), 1)
        self.assertEqual(req_a[0].tokens["output"], 40)  # the complete value, not 10 + 40
        self.assertEqual(req_a[0].tokens["cache_read"], 10000)
        self.assertAlmostEqual(req_a[0].cost, REQ_A, places=12)

    def test_turns_follow_prompt_ids(self):
        turns, _ = load(self.root, sessions=["session"])
        self.assertEqual([t.prompt_id for t in turns], ["p1", "p2", "p3"])
        t1 = self.turn(turns, "p1")
        self.assertEqual(t1.origin, "human")
        self.assertEqual(t1.prompt, "Fix the failing test in parser.py")
        self.assertEqual(t1.duration_s, 65.0)  # 10:00:00 prompt -> 10:01:05 last request

    def test_subagent_is_attached_to_the_turn_that_spawned_it(self):
        turns, _ = load(self.root, project=DEMO)
        t1 = self.turn(turns, "p1")
        self.assertEqual(t1.subagents, 1)
        self.assertEqual(sorted(r.request_id for r in t1.requests),
                         ["req_A", "req_B", "req_C", "req_S1", "req_S2"])
        self.assertEqual(sum(r.sidechain for r in t1.requests), 2)
        self.assertAlmostEqual(sum(r.cost for r in t1.requests), TURN_1, places=12)
        # sub-agent timestamps do not stretch the turn's duration
        self.assertEqual(t1.end.isoformat(), "2026-01-10T10:01:05+00:00")

    def test_retries_count_tool_results_in_error_including_subagents(self):
        turns, _ = load(self.root, project=DEMO)
        self.assertEqual(self.turn(turns, "p1").retries, 2)
        self.assertEqual(self.turn(turns, "p2").retries, 0)

    def test_waste_aborted_request_and_idle_turn(self):
        turns, _ = load(self.root, project=DEMO)
        t2, t3 = self.turn(turns, "p2"), self.turn(turns, "p3")
        self.assertFalse(t2.idle)
        self.assertAlmostEqual(waste(t2)["usd"], REQ_D, places=12)
        self.assertEqual(waste(t2)["tokens"], 20 + 21000 + 7)
        self.assertTrue(t3.idle)
        self.assertAlmostEqual(waste(t3)["usd"], TURN_3, places=12)
        self.assertEqual(waste(self.turn(turns, "p1"))["usd"], 0.0)

    def test_synthetic_api_error_line_is_not_a_request(self):
        turns, stats = load(self.root, project=DEMO)
        t2 = self.turn(turns, "p2")
        self.assertEqual([r.request_id for r in t2.requests], ["req_D", "req_D2"])
        self.assertEqual(t2.api_errors, 1)
        self.assertEqual(stats.synthetic_lines, 1)

    def test_request_copied_into_a_continued_session_is_counted_once(self):
        turns, stats = load(self.root)
        req_a = [r for t in turns for r in t.requests if r.request_id == "req_A"]
        self.assertEqual(len(req_a), 1)
        self.assertEqual(req_a[0].source.endswith("/session.jsonl"), True)
        self.assertEqual(stats.duplicate_requests, 1)
        continued = [t for t in turns if t.session_id == "continued-session"]
        self.assertEqual([t.prompt_id for t in continued], ["q1"])  # the copied turn is dropped
        self.assertAlmostEqual(sum(r.cost for r in continued[0].requests), REQ_O, places=12)

    def test_session_filter_still_drops_copied_requests(self):
        turns, _ = load(self.root, sessions=["continued-session"])
        self.assertEqual([t.prompt_id for t in turns], ["q1"])
        self.assertEqual([r.request_id for r in turns[0].requests], ["req_O"])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.ctx = FixtureDir()
        self.root = self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__()

    def test_since_keeps_turns_started_after(self):
        turns, _ = load(self.root, since=datetime(2026, 1, 10, 10, 4, tzinfo=timezone.utc))
        self.assertEqual([t.prompt_id for t in turns], ["p2", "p3", "q1", "x1"])

    def test_project_by_slug_and_by_path(self):
        by_slug, _ = load(self.root, project=OTHER)
        by_path, _ = load(self.root, project="/tmp/other-project")
        self.assertEqual([t.session_id for t in by_slug], ["elsewhere-session"])
        self.assertEqual([t.prompt_id for t in by_path], [t.prompt_id for t in by_slug])

    def test_unknown_project_is_an_error(self):
        with self.assertRaises(TranscriptError):
            load(self.root, project="-no-such-project")

    def test_session_filter(self):
        turns, _ = load(self.root, sessions=["elsewhere-session"])
        self.assertEqual([t.prompt_id for t in turns], ["x1"])
        with self.assertRaises(TranscriptError):
            load(self.root, sessions=["no-such-session"])

    def test_parse_since(self):
        now = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_since("7d", now), datetime(2026, 1, 3, 12, 0, tzinfo=timezone.utc))
        day = parse_since("2026-01-08")
        self.assertEqual((day.year, day.month, day.day, day.hour), (2026, 1, 8, 0))

    def test_project_slug_matches_claude_code(self):
        self.assertEqual(project_slug("/Users/me/.claude"), "-Users-me--claude")
        self.assertEqual(project_slug("/tmp/other_project"), "-tmp-other-project")


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.prices = load_prices(TEST_PRICES)
        self.tokens = {"input": 1000, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 1000}

    def test_unknown_model_is_a_blocking_error_naming_it(self):
        with FixtureDir() as root:
            with self.assertRaises(UnknownModelError) as ctx:
                load(root, since=None, project=DEMO)
        self.assertIn("claude-mystery-9", str(ctx.exception))
        self.assertEqual(ctx.exception.models, {"claude-mystery-9": 1})

    def test_unknown_model_outside_the_window_does_not_block(self):
        with FixtureDir() as root:
            turns, _ = load(root, sessions=["session"])  # SINCE excludes the 2026-01-05 turn
        self.assertAlmostEqual(sum(r.cost for t in turns for r in t.requests), TURN_1 + TURN_2 + TURN_3, places=12)

    def test_alias_fast_mode_and_geography(self):
        self.assertAlmostEqual(self.prices.cost("claude-test-l", self.tokens), (1000 + 10000) * M)
        self.assertAlmostEqual(self.prices.cost("claude-test-large", self.tokens, speed="fast"), (2000 + 20000) * M)
        self.assertAlmostEqual(self.prices.cost("claude-test-large", self.tokens, inference_geo="us"),
                               (1000 + 10000) * M * 1.1)

    def test_unpriced_speed_or_geography_is_an_error(self):
        with self.assertRaises(PricingError):
            self.prices.cost("claude-test-small", self.tokens, speed="fast")
        with self.assertRaises(PricingError):
            self.prices.cost("claude-test-large", self.tokens, speed="turbo")
        with self.assertRaises(PricingError):
            self.prices.cost("claude-test-large", self.tokens, inference_geo="mars")

    def test_repository_prices_file_is_dated_and_sourced(self):
        prices = load_prices()
        self.assertRegex(prices.retrieved, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(prices.sources["prices"].startswith("https://"))


class WebSearchAndIncompleteUsageTests(unittest.TestCase):
    """tests/fixtures/web-session: WebSearch tool results with searchCount, and a sub-agent
    request whose lines all have stop_reason null (written before its final usage)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cost-meter-test-")
        project = Path(self.tmp) / "-tmp-web-project"
        project.mkdir()
        shutil.copy(FIXTURES / "web-session.jsonl", project / "web-session.jsonl")
        shutil.copytree(FIXTURES / "web-session", project / "web-session")
        self.root = Path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_web_searches_are_priced_on_the_request_that_called_the_tool(self):
        turns, _ = load(self.root, since=None)
        reqs = {r.request_id: r for t in turns for r in t.requests}
        self.assertEqual({k: r.web_search_count for k, r in reqs.items()},
                         {"req_W1": 2, "req_W2": 0, "req_T1": 1, "req_T2": 0})
        for rid, expected in (("req_W1", REQ_W1), ("req_W2", REQ_W2), ("req_T1", REQ_T1), ("req_T2", REQ_T2)):
            self.assertAlmostEqual(reqs[rid].cost, expected, places=12, msg=rid)
        self.assertEqual(turn_record(turns[0])["web_searches"], 3)

    def test_request_without_final_usage_is_counted_not_guessed(self):
        turns, stats = load(self.root, since=None)
        reqs = {r.request_id: r for t in turns for r in t.requests}
        self.assertTrue(reqs["req_T1"].incomplete_usage)
        self.assertEqual(reqs["req_T1"].tokens["output"], 1)  # the partial count, not an estimate
        self.assertFalse(reqs["req_W1"].incomplete_usage)  # its second line carries the final usage
        self.assertEqual(reqs["req_W1"].tokens["output"], 20)
        self.assertEqual(stats.incomplete_usage, 1)
        code, out, _ = run_cli("--projects-dir", str(self.root), "--prices", TEST_PRICES)
        self.assertEqual(code, 0)
        self.assertIn("1 requests written without their final usage", out)

    def test_lines_without_the_stop_reason_field_are_not_flagged(self):
        with FixtureDir() as root:
            _, stats = load(root, sessions=["session"])
        self.assertEqual(stats.incomplete_usage, 0)

    def test_web_search_without_a_price_is_an_error(self):
        data = json.loads(Path(TEST_PRICES).read_text(encoding="utf-8"))
        del data["web_search_per_1000_searches"]
        prices_path = Path(self.tmp) / "prices-no-search.json"
        prices_path.write_text(json.dumps(data), encoding="utf-8")
        prices = load_prices(str(prices_path))
        tokens = {"input": 1, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 1}
        self.assertAlmostEqual(prices.cost("claude-test-large", tokens), 11 * M)  # no search: no price needed
        with self.assertRaises(PricingError):
            prices.cost("claude-test-large", tokens, web_searches=1)

    def test_geography_multiplier_does_not_apply_to_the_search_fee(self):
        prices = load_prices(TEST_PRICES)
        tokens = {"input": 1000, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 0}
        self.assertAlmostEqual(prices.cost("claude-test-large", tokens, inference_geo="us", web_searches=1),
                               1000 * M * 1.1 + SEARCH)


class ReportAndCliTests(unittest.TestCase):
    def setUp(self):
        self.ctx = FixtureDir()
        self.root = self.ctx.__enter__()

    def tearDown(self):
        self.ctx.__exit__()

    def test_groupings_add_up(self):
        turns, _ = load(self.root)
        total = totals(turns)["cost_usd"]
        self.assertAlmostEqual(total, TURN_1 + TURN_2 + TURN_3 + REQ_O + REQ_X, places=12)
        for by in ("session", "model", "project", "day"):
            rows = group(turns, by)
            self.assertAlmostEqual(sum(r["cost_usd"] for r in rows), total, places=12, msg=by)
        models = {r["key"]: r for r in group(turns, "model")}
        self.assertAlmostEqual(models["claude-test-small"]["cost_usd"], REQ_S1 + REQ_S2 + REQ_X, places=12)

    def test_cli_json_output(self):
        code, out, _ = run_cli("--projects-dir", str(self.root), "--prices", TEST_PRICES,
                               "--since", "2026-01-08", "--by", "turn", "--json", "-")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["by"], "turn")
        self.assertEqual(len(doc["turns"]), 5)
        self.assertAlmostEqual(doc["totals"]["cost_usd"], TURN_1 + TURN_2 + TURN_3 + REQ_O + REQ_X, places=12)
        t1 = next(t for t in doc["turns"] if t["prompt_id"] == "p1")
        self.assertEqual((t1["retries"], t1["subagents"], t1["tool_uses"]), (2, 1, 3))
        self.assertEqual(t1["tokens"]["cache_write_1h"], 2000)

    def test_cli_table_and_csv(self):
        code, out, _ = run_cli("--projects-dir", str(self.root), "--prices", TEST_PRICES, "--since", "2026-01-08")
        self.assertEqual(code, 0)
        self.assertIn("Total: 3 sessions, 5 turns, 10 requests", out)
        code, out, _ = run_cli("--projects-dir", str(self.root), "--prices", TEST_PRICES, "--since", "2026-01-08",
                               "--by", "day", "--csv", "-")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("key,turns,requests,input,"))

    def test_cli_unknown_model_exits_non_zero_and_names_it(self):
        code, out, err = run_cli("--projects-dir", str(self.root), "--prices", TEST_PRICES)
        self.assertEqual(code, 2)
        self.assertIn("claude-mystery-9", err)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
