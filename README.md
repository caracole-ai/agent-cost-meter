# agent-cost-meter

What did that task cost? `cost-meter` reads the transcripts Claude Code already writes on your
machine and reports, **per task**: cost, tokens, retries, waste and duration.
It uses only the Python 3 standard library (3.9 or later), sends nothing over the network, and
does not change the transcripts.

Agent benchmarks publish pass rates, and sometimes the cost of a single generation. They rarely
publish the **cost of a task carried through to the end in a real session**: every request,
sub-agents included. This tool measures that number from your own sessions.

## Usage

```
bin/cost-meter [--since 7d|YYYY-MM-DD] [--project <slug|path>] [--session <id>]...
               [--by turn|session|model|project|day]
               [--json out.json] [--csv out.csv] [--prices prices.json] [--projects-dir DIR]
```

```sh
git clone <this repository> && cd agent-cost-meter
bin/cost-meter --since 7d                        # one row per session, then the totals
bin/cost-meter --since 7d --by turn              # one row per task, with the first 80 characters of its prompt
bin/cost-meter --project ~/code/my-app --by day
bin/cost-meter --session <session-id> --json -   # everything as JSON on stdout, for scripts
python3 -m unittest discover -s tests -t .       # the test suite (synthetic fixtures only)
```

- `--since 7d` covers the last 7 × 24 hours; `--since 2026-09-15` starts at local midnight. A task
  is kept when it **started** inside the window.
- `--project` takes the directory name under `~/.claude/projects/` or the working directory
  path (converted the way Claude Code converts it: every character other than a letter or a digit becomes `-`).
  An unknown project is an error, not an empty table.
- `--session` (repeatable) keeps some sessions only. Tools can use it to measure one run.
- `--json` writes the totals, the grouped rows, every task (`turns`) and read statistics.
  `--csv` writes the grouped rows. `-` means stdout.
- `--projects-dir` defaults to `$CLAUDE_CONFIG_DIR/projects`, or `~/.claude/projects` if that variable is not set.
- Exit code 2 on any error (unknown model, missing prices file, unknown project or session).

The table has no colors and uses no dependency. Token columns are rounded (`12.3M`). JSON and CSV
keep exact integers.

## Definitions

These are the exact rules the code applies (`cost_meter/transcripts.py`, `cost_meter/report.py`).

- **Task (turn)**: one human turn. Claude Code writes the same `promptId` on every `user`
  line of a turn: the prompt and each tool result. A new `promptId` starts a new task, and
  everything written until the next one belongs to it. Sub-agents run inside the task that
  spawned them: the sub-agent's `meta.json` names the `tool_use` that launched it. If that
  link is missing, the sub-agent's `promptId` is used. Turns that Claude Code starts
  on its own (a background agent finishing, `origin: task-notification`) are also tasks.
  They are labelled with their `origin` so you can filter them out.
- **Request**: one API call, identified by its `requestId`. Claude Code writes a request on
  one line per content block and **repeats its `usage` on each line**. Only `output_tokens`
  grows as the response streams. Each request is counted **once**, using the largest value
  of each token field. When a request is copied into a later session file (a continued session),
  only the earliest session counts it.
- **Cost** = Σ over distinct requests of
  `input × input price + 5-min cache writes × input price × 1.25 + 1-hour cache writes × input price × 2
  + cache reads × input price × cache-read multiplier + output × output price
  + web searches × search fee`.
  Every price and multiplier comes from `prices.json`. Web searches are read from the WebSearch
  tool result (`toolUseResult.searchCount`) and charged to the request that called the tool. Fast mode (`usage.speed: "fast"`) uses the
  model's fast-mode prices. `usage.inference_geo: "us"` applies its multiplier.
  **A model missing from `prices.json` stops the run with an error that names it.** A request is never silently
  priced at zero. This check applies only to models used inside the selected window. A web search
  with no search price in `prices.json` is an error too.
- **Requests without their final usage.** A request's final usage arrives with its `stop_reason`.
  When every line of a request has `stop_reason: null`, the line was written before that final usage
  and `output_tokens` is a partial count (often 1 to 10). The meter keeps the partial count, does not
  guess, and reports how many such requests it read (`read_stats.incomplete_usage`, and a note under
  the table).
- **Retries** = `tool_result` blocks with `is_error: true` in the task, sub-agents included.
  This counts failed commands, rejected edits and denied permissions.
- **Waste** = the cost (and tokens) of requests aborted mid-stream (`isAbortedMidStream`),
  plus the whole cost of an **idle** task: one whose main-thread requests produced
  neither a tool call nor any non-empty text.
- **Duration** = first to last timestamp of the task's main-thread lines (the prompt, tool
  results, requests). Sub-agent lines do not extend it. It is wall-clock time and includes
  time spent waiting for you, for example at a permission prompt.
- `<synthetic>` lines (`message.model == "<synthetic>"`) are messages Claude Code writes
  itself, such as a 429 rate-limit error. They carry zero tokens and are counted as
  `api_errors`, not as requests.

## Prices

`prices.json` is dated and sourced. It records the Anthropic first-party API list prices, read on
**2026-09-22** from the official page <https://platform.claude.com/docs/en/about-claude/pricing>.
The model IDs come from <https://platform.claude.com/docs/en/about-claude/models/overview>.

| Model ID | Input $/MTok | Output $/MTok | Cache read | Notes |
|---|---|---|---|---|
| `claude-fable-5-1` | 10 | 50 | 0.025× input | |
| `claude-opus-5` | 5 | 25 | 0.1× | fast mode 10 / 50 |
| `claude-sonnet-5` | 2 | 10 | 0.1× | |
| `claude-haiku-4-5-20251001` (alias `claude-haiku-4-5`) | 1 | 5 | 0.1× | |

Cache writes cost 1.25× the input price for the 5-minute TTL and 2× for the 1-hour TTL, on every
model above. Web search costs 10 USD per 1,000 searches (`web_search_per_1000_searches`), on top of
tokens. It is a fee per search, so the `inference_geo` multiplier, which applies to tokens, is not applied to it. The same page states that Claude 4.6 and later models are billed at standard rates across the
full 1M-token context. That is why a `[1m]` context variant has no separate price.

Only models whose ID is confirmed on the models overview page are listed. Older models (Opus 4.x,
Sonnet 4.x, Haiku 3.5) have prices on the pricing page, but their exact IDs are not on the overview
page. They are left out on purpose. If a transcript uses one, add it from the official pages.
Guessing is not allowed.

To update prices, edit `prices.json` and change `retrieved`. The code contains no price.

## Limits

- **API-equivalent cost.** The figure is what the tokens would cost at list API prices.
  On a Claude Pro or Max subscription, you do not pay that amount. The figure measures
  consumption against your quota, not your bill. Bedrock and Vertex have their own prices,
  and Batch API discounts do not apply to interactive sessions.
- **Only what the transcript records.** Claude Code makes requests that it never writes to the
  transcript (see the cross-check below). They are **not counted**, and they cannot be recovered
  from the transcripts. Sub-agent requests written before their final usage (see Definitions)
  have their output under-counted.
- **The transcript format is undocumented.** The rules above come from transcripts observed on
  2026-09-22 (Claude Code 2.1.268 to 2.1.278). A format change can break them. The fixtures in `tests/fixtures/`
  record the structure the code expects.
- An advisor model (`advisorModel` on assistant lines) was configured in the observed sessions,
  but no advisor call or usage appeared in their transcripts. An advisor call that does not
  appear in the transcript is not measured.

## Cross-check (2026-09-22, the author's machine)

- **`~/.claude/stats-cache.json`: not cross-checked.** Its daily token totals
  (`dailyModelTokens`) stop at 2026-09-14 (`lastComputedDate` 2026-09-15), so they do not cover the
  7-day window of the gate run (2026-09-15 → 2026-09-22). On the days where both sources exist, its totals are
  4.1 to 5.5 times the de-duplicated token count. On 2026-09-14, summing `usage` over every
  transcript line, without de-duplication, lands much closer. It is not a reference for cost per request.
- **`cost-state` lines: cross-checked, every dollar of the gap has a named cause.** Claude Code
  writes a `cost-state` line in the session file with its own running cost (`totalCostUSD`, and
  tokens and `costUSD` per model). It is a per-process ledger: every API call the process makes is
  added to it, including calls that are never written to the transcript. It is reset by `/clear`
  and `/login`, restored from the last `cost-state` line on `--resume`, and carried into the new
  session when a session continues in another one (`continued-in` line).

  Comparison on the 67 sessions active since 2026-09-15 13:25 that have a `cost-state` line
  (whole sessions, not only turns inside the window). v0.1.0 of the meter: 2,035.43 USD, Claude Code:
  2,344.69 USD (+15.2 %). The meter now prices web searches: 2,037.80 USD (+15.1 %).

  | Cause (USD, 67 sessions) | Amount | Recoverable from the transcripts? |
  |---|---:|---|
  | Web-search fees: 237 searches, each in a WebSearch tool result (`searchCount`) | 2.37 | **Yes, now priced**. The counts match `cost-state` on all 187 sessions checked. |
  | `cost-state` of a continued session includes its predecessor's cost (`continued-in`), so a sum over sessions counts it twice | −2.16 | Nothing to recover: this is a double count in `cost-state` |
  | Requests interrupted by the user (no final usage): Claude Code does not add them to its ledger, but their input was processed | −2.68 | The meter keeps them. `cost-state` under-counts here. |
  | Haiku calls never written: WebFetch page processing, the WebSearch call itself, session titles | 10.72 | No |
  | Main-model cache reads never written: forked calls that re-read the context. `agent_summary` runs every 30 s for each running sub-agent; `prompt_suggestion` and `away_summary` run at the end of a turn; compaction also re-reads it | 210.78 | No |
  | Main-model uncached input never written: the prompts those forks append without caching them | 13.86 | No |
  | Main-model output missing: sub-agent requests written before their final usage (1,457 requests, about 1.65M tokens by estimate), plus the forks' own output | 45.15 | No: only the count of such requests is reported |
  | Main-model cache writes never written: compaction and memory-extraction forks | 26.90 | No |

  After these causes, nothing is left unexplained: the rows add up to the gap to the cent. The four
  sessions with the largest gaps are long sessions with many sub-agents. Their gaps are
  90.16, 84.53, 54.44 and 45.33 USD, and 189.43 USD of the total are forked cache reads. The
  per-cause split between forks comes from token types and
  timing (a 30-second `agent_summary` simulation gives 1.13 to 1.19× the missing cache reads on
  three of the four sessions), not from records. The fork names and `skipTranscript` come from the
  Claude Code 2.1.278 binary. Across all 187 sessions with a `cost-state` line, two more effects
  show up. A `/login` in the middle of a session resets the ledger (one session, −51.10 USD in
  `cost-state`). A sub-agent that is still running when its session continues in the background is
  booked to the new session's ledger (one pair).
  The prices agree: Claude Code prices 1-hour cache writes at 2× and searches at 0.01 USD, as
  `prices.json` does. So the meter remains a **floor**: about 13 % below Claude Code's own ledger
  on these sessions. Nearly all of the difference comes from calls that Claude Code never writes to
  the transcript.

## Related

- [`agent-guardrails`](https://github.com/caracole-ai/agent-guardrails): the Claude Code hooks
  (destructive-command guard, definition of done, post-edit checks) whose sessions this meter measures.

## En français

`cost-meter` lit les transcripts que Claude Code écrit déjà sur votre machine. Pour chaque tâche
(un tour humain), il donne le coût, les tokens, les reprises (résultats d'outil en erreur), le
gaspillage (requêtes interrompues, tours sans action ni texte) et la durée. Il n'utilise que la
bibliothèque standard de Python 3.9 et n'envoie rien sur le réseau.

- Le coût est calculé **une fois par `requestId`**, car Claude Code répète le même `usage` sur
  plusieurs lignes.
- Les prix viennent de `prices.json`, relevé le 2026-09-22 sur la page officielle d'Anthropic.
  Un modèle absent de ce fichier arrête le calcul avec une erreur qui le nomme.
- C'est un **coût API équivalent**. Sur un abonnement, il mesure la consommation du quota, pas la
  facture.
- Les recherches web sont facturées (10 USD les 1 000) à partir du `searchCount` des résultats
  WebSearch.
- C'est un **plancher**. Sur les 67 sessions recoupées avec les lignes `cost-state`, le compteur
  est 15,1 % en dessous du grand livre de Claude Code, et tout l'écart a une cause identifiée. Pour
  l'essentiel, ce sont des appels que Claude Code ne transcrit jamais : résumés de sous-agents
  toutes les 30 s, suggestions de fin de tour, appels Haiku de WebFetch, de WebSearch et des titres,
  compaction. S'y ajoutent des sous-agents écrits avant leur usage final. Aucun de ces coûts ne
  peut être retrouvé dans les transcripts. `stats-cache.json` n'est pas recoupé : il s'arrête au
  2026-09-14, et ses totaux ne sont pas dédoublonnés.

## License

MIT, see [LICENSE](LICENSE).
