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
  + cache reads × input price × cache-read multiplier + output × output price`.
  Every price and multiplier comes from `prices.json`. Fast mode (`usage.speed: "fast"`) uses the
  model's fast-mode prices. `usage.inference_geo: "us"` applies its multiplier.
  **A model missing from `prices.json` stops the run with an error that names it.** A request is never silently
  priced at zero. This check applies only to models used inside the selected window.
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
model above. The same page states that Claude 4.6 and later models are billed at standard rates across the
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
- **Only what the transcript records.** Claude Code makes some requests that it does not write
  to the transcript. `cost-state` lines report Haiku usage and web searches that the
  transcript does not contain (probably the model calls behind WebFetch/WebSearch and session
  titles), and more (see the cross-check below). Those are **not counted**, and web-search fees
  (10 USD per 1,000 searches) are not priced.
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
- **`cost-state` lines: cross-checked, the meter reads low.** Claude Code writes a
  `cost-state` line in some session files, with its own `totalCostUSD` per session. Over the 67
  sessions of the window that have one, Claude Code's total is **15.2 % higher** than the
  meter's. **14 of the 67 sessions match to the cent.** The gap is tokens that are not in the transcript:
  the per-session token totals in `cost-state` are higher on every category, uncached input most of
  all. Part of the gap is identified: Haiku calls and 237 web searches that the transcripts do not contain. Most of it sits
  in four long, sub-agent-heavy sessions and is **not explained**. The prices themselves agree:
  on the sessions checked, pricing `cost-state`'s own token counts with `prices.json` gives back its `costUSD` to within a few cents.
  So treat the meter's figure as a **floor**. When a session has a `cost-state` line, compare against it.

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
- C'est un **plancher**. Claude Code fait des requêtes qu'il n'écrit pas dans le transcript. Sur
  les 67 sessions recoupées avec ses lignes `cost-state`, le compteur est 15 % en dessous
  (14 sessions identiques au centime). `stats-cache.json` n'est pas recoupé : il s'arrête au
  2026-09-14, et ses totaux ne sont pas dédoublonnés.

## License

MIT, see [LICENSE](LICENSE).
