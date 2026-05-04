# langgraph-multi-agent

A LangGraph harness for **validating open-weight LLMs as agentic coding agents**, with the eventual target of running everything locally on a 32 GB AMD R9700 (or dual-R9700). Built to test whether a model can drive a full multi-file project end-to-end without thrashing.

## Architecture

Two layers of state machines.

**Outer (PBE — planner / builder / evaluator):**

```
planner → builder → [router] → evaluator → [router] → planner | END
                       ↑                       ↓
                       └────────── continue ───┘
                       └────────── replan ─────┘
```

- **Planner** — Claude Sonnet 4.6 (Anthropic API). Writes a structured plan and the explicit prompts the builder and evaluator receive. The other models don't get generic system prompts; they get prose authored by Sonnet for this specific iteration. Has a startup short-circuit (`route_after_planner`): if the prior task's trace shows `verification_token_consumed` followed by `builder_exit reason="done"` and the new input is a trivial continuation (`continue`, `go`, `proceed`, …), the planner emits `path="already_complete"` and the harness terminates without invoking the builder. Stops the planner from inventing fictional new requirements on a working codebase.
- **Builder** — Qwen3-Coder-Next via OpenRouter. Custom `StateGraph` (not `create_react_agent`) with a sophisticated tool surface (see below).
- **Evaluator** — Qwen3.6-27B (vision-capable) via OpenRouter, with read-only file tools + Playwright MCP browser tools loaded over a **persistent** SSE session (each MCP tool call shares the same browser page; the default `client.get_tools()` mode opens a fresh session per call and loses page state between tool calls — broken for any verification flow). Verifies builder output via both code (`npm run build`) and browser interaction (navigate / screenshot / snapshot / console_messages / click), guided by a mandatory interaction protocol on web-app tasks.
- **Advisor** — Claude Sonnet 4.6 again, gating builder completion. The builder cannot call `mark_done` directly; it must first call `verify_completion(task_summary, evidence, verify_command)` which sends the evidence + plan + recent verify-output to the advisor. The advisor returns a structured verdict including `next_actor`: `builder_continue` (default rejection — code-level work to do), `needs_evaluator` (work looks reasonable but needs browser-based verification), or `builder_disagreement` (wrong-problem; planner re-engages). The harness routes accordingly: `needs_evaluator` short-circuits the builder loop and hands off to the evaluator; `builder_disagreement` routes to the planner under the existing replan cap.

**Inner (builder StateGraph):**

```
START → model → tools → [router] → model | END
```

- **Persistent bash session** via `pexpect` — `cd`, `export`, venv activations all survive across `shell()` calls. Spawned with a noninteractive env (`CI=true`, `npm_config_yes=true`, `NEXT_TELEMETRY_DISABLED=1`, etc.) so npm/npx/prisma never reach a prompt. Timeouts trigger an escalating kill (SIGINT → SIGQUIT → respawn) and unconditional reset, so the next call always sees a clean session.
- **Patch-based file editor**: `view_file` (line-numbered), `str_replace` (unique-match required), `create_file` (errors if exists). No `write_file` — full-file overwrites were the worst pathology in the previous design.
- **Structured plan in state** (v2 schema): requirements + architecture (`stack` / `file_tree` / `data_model` / `key_decisions`, or `summary` for non-coding tasks) + tasks. Plan re-renders into the system message every turn. Builder mutates tasks via `update_plan_item` / `add_plan_item` and can flag architecture changes for planner review via `propose_architecture_change` (queues to a `pending_proposals` list — planner accepts/rejects on next iteration). Capped at `MAX_REPLANS=2` builder-triggered replans per task.
- **Step budget** rendered into every model turn (`Step 14 of 50, 36 tool calls remaining`), escalating to BUDGET WARNING and FINAL STEP.
- **Verification gate**: builder cannot exit by trailing off — must call `mark_done(verify_command, claim, verification_token)` which actually runs the verify command and only exits on exit code 0. The `verification_token` comes from a mandatory upstream `verify_completion(task_summary, evidence, verify_command)` call that routes the builder's evidence (plus the original task, locked architecture, current plan state, and recent verify-command stdout) to a Sonnet **advisor** for an external sanity check. Only a `done` verdict mints a single-use UUID token. Two caps: 3 advisor verdicts per task (cap reached → `give_up`, planner takes over) and a separate 2 advisor errors (Anthropic outage / unparseable response — `request_user_help`). Errors don't burn the verdict cap. Two other clean exits unchanged: `request_user_help`, `give_up`.
- **Stuck detector** — three heuristics (edit churn, build-error stagnation, tool repetition) with thresholds named at the top of `graph.py`.
- **Per-edit syntax check** for `.py` (`py_compile`) and `.js/.cjs/.mjs` (`node --check`). TS/TSX deferred — single-file checks aren't meaningful for cross-file imports.
- **Smart truncation** (head + tail with byte-elision marker) on shell output.
- **Live progress** while things are running: planner and builder token-stream their output to stdout under `[planner]` / `[builder]` prefixes; long-running tools emit heartbeat ticks every 20s (`·· shell [40s, 4096B, +1024, last: "Downloading next@14..."]`), with `STUCK` flagged when stdout-bytes don't grow between ticks. All ticks also land in the trace as `tool_progress` events. The evaluator stage uses a longer 60s heartbeat threshold because verdict composition (natural-language synthesis over many tool observations) routinely takes 30-60s and shouldn't pollute the trace with idle warnings.

**Skill files:** the planner / builder / evaluator / advisor system prompts live in `skills/<role>/SKILL.md` rather than embedded Python strings. Each is editable independently. The evaluator skill in particular has hard rules the harness enforces: a mandatory interaction protocol (`browser_navigate` + `browser_take_screenshot` + `browser_snapshot` + `browser_console_messages` per plan-named page, plus admin-flow click verification) and an explicit budget pacing rule (50/30/20 split: explore → write verdict → optional follow-up). Verdicts that violate either get rejected by the harness at the verdict-validation layer (see Resilience).

**Trace logging:** every tool call, tool result, state transition, stuck-detector firing, plan update, and exit reason is written as one line of JSONL to `workspace/.trace/<UTC>-<slug>.jsonl`. Without this you can't tell if harness changes actually helped.

**Screenshot inspection:** the playwright-mcp container's `/tmp/.playwright-mcp/` is bind-mounted to `workspace/.playwright-mcp/` on the host. Screenshots, snapshot YAMLs, and console-message dumps from the evaluator land there in real time and are readable from your Mac without `docker cp`.

## Quick start

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and OPENAI_API_KEY (OpenRouter)

docker compose build
./run.sh
```

`./run.sh` is a thin wrapper for `docker compose run --rm --use-aliases --service-ports langgraph`. Both flags are required (the wrapper sets them for you, and `graph.py`'s startup self-check will warn loudly if you invoke without `--use-aliases`):
- `--use-aliases`: without it, the transient `compose run` container only registers its container-name on the project network, so the `playwright-mcp` sibling can't resolve `langgraph:3000` for `browser_navigate` calls and Firefox returns `NS_ERROR_UNKNOWN_HOST`. With it, the run container picks up the service-name DNS alias.
- `--service-ports`: `compose run` ignores the service's `ports:` mapping by default (a known compose-run-vs-up difference). Without this, the dev server the builder spawns is reachable inside the container and from playwright-mcp, but NOT from your host browser at `http://localhost:3000`. With it, the declared port is published.

Then at the `Task:` prompt, give it a coding task. Each task runs the full PBE loop (max 5 iterations) and emits a trace file you can grep.

## Configuration

All in `.env`:

| Var | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sonnet planner + advisor | (required) |
| `PLANNER_MODEL` | Anthropic model ID for the planner | `claude-sonnet-4-6` |
| `ADVISOR_MODEL` | Anthropic model ID for the completion advisor | `claude-sonnet-4-6` |
| `OPENAI_API_KEY` | OpenRouter | (required) |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | `https://openrouter.ai/api/v1` |
| `BUILDER_MODEL` | Builder slug | `qwen/qwen3-coder-next` |
| `EVAL_MODEL` | Evaluator slug (must be vision-capable) | `qwen/qwen3.6-27b` |
| `OPENROUTER_PROVIDERS` | Comma-separated provider pin (priority order, fallbacks disabled) | (unset) |
| `OPENROUTER_IGNORE_PROVIDERS` | Comma-separated providers to exclude (other providers still tried) | (unset) |
| `PLAYWRIGHT_MCP_URL` | MCP server SSE URL | `http://playwright-mcp:8931/sse` |
| `STREAM_CHUNK_TIMEOUT_SECONDS` | Per-chunk model-stream timeout (seconds) | `60` |

OpenRouter routes flakily for tool-calling on some providers (the model returns native XML format, the provider doesn't translate it back). If you see broken tool calls, find a working provider in your OpenRouter activity log and pin via `OPENROUTER_PROVIDERS=...`. To exclude a single bad provider without pinning everything, use `OPENROUTER_IGNORE_PROVIDERS=parasail` (etc.) — the rest of the fallback set still runs.

The `local` compose profile starts a llama.cpp server alongside, for the eventual move off OpenRouter. See `docker-compose.yml`.

## Resilience

- **Model retry**: every planner / builder model call is wrapped in `_ainvoke_streaming`, which retries the full astream on transient upstream errors (HTTP 5xx, connection drops, asyncio timeouts) up to `MODEL_RETRY_MAX_ATTEMPTS=3` with exponential backoff (2s / 4s / 8s). Partial chunks from failed attempts are discarded; the returned `AIMessage` is always assembled from a single successful stream. Each retry shows `↻ <label> retry N/M in Ks (ErrorType: ...)` on stdout and a `model_retry` event in the trace. 4xx errors and 429 are NOT retried (TODO: 429 needs Retry-After parsing). A separate `STREAM_CHUNK_TIMEOUT_SECONDS=60` catches stuck streams faster than langchain's 120s default.
- **Checkpoint resume**: outer + inner graph state is persisted to `workspace/.trace/checkpoints.db` (SQLite) at every node boundary. On crash, the next start scans for unfinished tasks (last trace event isn't `task_end`, file modified <24h ago) and prompts `Resume? [y/N]` (default N — fresh runs are fresh by default; same-task-text does NOT auto-resume). On resume, the inner builder graph picks up at the exact step it crashed at, not from step 0.
- **Schema versioning**: each saved checkpoint is stamped with `CHECKPOINT_SCHEMA_VERSION` in metadata. **Bump it whenever the `State` or `BuilderState` TypedDict shape changes** (in `graph.py`). Mismatched checkpoints are rejected with a `checkpoint_schema_mismatch` trace event and the run starts fresh — no silent corruption from old state.
- **MCP infrastructure failures route to `verdict=incomplete` → END**: when an evaluator-side exception matches `EVAL_INCOMPLETE_EXCEPTION_PATTERNS` (Playwright not installed, NS_ERROR_UNKNOWN_HOST, ConnectError, anyio `ClosedResourceError` / `BrokenResourceError`, MCP transport closed, ECONNREFUSED, …), the harness sets `verdict=incomplete` and terminates the run with a diagnostic. Without this, MCP transport drops mid-eval used to map to `verdict=continue` and loop the builder on infrastructure problems it couldn't fix.
- **Per-tool errors are recoverable**: MCP tools are loaded with `handle_tool_error=True`, so a `ToolException` raised by a single tool (e.g. `File access denied` on a screenshot with a bad filename, `element not found` on a stale ref) becomes the tool result instead of crashing the subagent. The model reads the error and retries with different args. Real infrastructure failures still bypass this path and hit the `incomplete` route above.
- **Persistent MCP session**: the evaluator opens `client.session("playwright")` once at first use and binds tools to that session via `load_mcp_tools(session)` (mode 2 of `langchain-mcp-adapters`). The default `client.get_tools()` opens a fresh session per tool call, which means each MCP tool gets a fresh Playwright page and page state is destroyed between calls — `browser_navigate` succeeds, then `browser_snapshot` returns `about:blank`. The session is cleaned up via `AsyncExitStack` at process exit.
- **Multimodal MCP responses normalized**: `ToolMessage.content` from MCP browser tools is a list of content blocks (`{type: "text"}`, `{type: "image"}`) on `@playwright/mcp@0.0.73`. `_tool_msg_content_str()` flattens to a string for display + logging — without it, `.strip()` on a list crashed the eval streaming loop with `AttributeError`.
- **Verdict validation on web-app tasks**: two retry tracks reject thin verdicts inline before they propagate to the next stage.
  - *Empty NOTES*: a verdict block with under `EVAL_NOTES_MIN_CHARS=100` of stripped content is rejected with a corrective preamble that asks for findings from already-observed data (no more exploration). Cap 1; second empty notes → `verdict=incomplete` with salvaged findings folded in.
  - *Insufficient interaction evidence*: `verdict=done` on a web-app task without minimum browser-tool counts (`browser_navigate` ≥ 1, `browser_take_screenshot` ≥ 1, `browser_click` ≥ 2) gets rejected with a corrective preamble naming the missing minimums. Cap 2.
- **Findings salvage on recursion-limit timeout**: if the evaluator hits `EVAL_RECURSION_LIMIT=100` without writing a verdict block, `_extract_eval_findings()` scans the in-memory tool-history buffer for actionable patterns (`Console: N errors` with N>0, `Unhandled Runtime Error` overlays in snapshots, `browser_click` results landing on `/login` when the click target wasn't login-related, HTTP 4xx/5xx from curl) and folds them into the verdict notes. A budget-overrun run still produces actionable evidence for the next planner pass.

### Manual checkpoint-resume verification

There's no automated test for end-to-end resume (it requires a real model + a kill mid-builder), but the recipe is short:

1. `./run.sh` and give it a multi-step task that takes ≥30s of builder work (e.g. "scaffold a Next.js app with Prisma").
2. Watch the builder progress (`Step N of 50`). When you see, say, step 5 has executed, **kill the process** (Ctrl-C the docker run, or `docker kill` from another shell).
3. Confirm `workspace/.trace/checkpoints.db` exists and the most-recent trace `.jsonl` does NOT end with a `task_end` line: `tail -1 workspace/.trace/<latest>.jsonl | jq .kind` → not `"task_end"`.
4. `./run.sh` again. Expect: `Found unfinished task from Nm ago: '<task text>' / Last event: <kind> / Resume? [y/N]:`. Type `y`.
5. Verify in stdout that the builder step counter resumes at ≥6 (the exact post-crash checkpoint), not 1. The trace will show a `task_resume` event followed by the existing iteration's events continuing.

If step 5 shows the counter at 1, checkpointing isn't actually working — check that `_graph_holder["builder"]` is being replaced in `main()`.

For automated resilience tests (model retry classification, schema mismatch detection, transient-error retry), run:

```bash
docker compose run --rm --use-aliases --service-ports langgraph python /app/test_resilience.py
```

## Tunable thresholds

All of these are named constants at the top of `graph.py`:

```python
MAX_PBE_ITERATIONS = 5
MAX_BUILDER_STEPS = 50
BUILDER_BUDGET_WARNING_THRESHOLD = 10
MAX_REPLANS = 2

EVAL_RECURSION_LIMIT = 100        # eval tool-call cap; budget-pacing rules in skill aim for ~80
EVAL_TOOL_HISTORY_FOR_FINDINGS = 30   # ring buffer scanned by _extract_eval_findings on timeout

# Eval verdict validation (web-app tasks only)
EVAL_MIN_NAVIGATE_CALLS = 1
EVAL_MIN_SCREENSHOT_CALLS = 1
EVAL_MIN_CLICK_CALLS = 2          # 1 menu + 1 admin minimum
EVAL_INSUFFICIENT_EVIDENCE_RETRY_CAP = 2
EVAL_NOTES_MIN_CHARS = 100        # below this, NOTES treated as empty
EVAL_EMPTY_NOTES_RETRY_CAP = 1    # 1 retry, then verdict=incomplete

# Eval idle thresholds: longer than builder/planner because verdict composition
# (natural-language synthesis) routinely takes 30-60s
EVAL_HEARTBEAT_THRESHOLD_SECONDS = 60
EVAL_HEARTBEAT_INTERVAL_SECONDS = 60

STUCK_EDIT_REPEAT_THRESHOLD = 3
STUCK_EDIT_WINDOW = 10
STUCK_BUILD_ERROR_REPEAT = 2
STUCK_BUILD_HISTORY = 3
STUCK_TOOL_REPEAT = 3             # bumped from 2; one back-to-back retry is normal recovery
STUCK_INJECTION_CAP = 3

SHELL_COMMAND_TIMEOUT_SECONDS = 300
SHELL_OUTPUT_HEAD_BYTES = 2000
SHELL_OUTPUT_TAIL_BYTES = 5000
SHELL_KILL_SIGINT_WAIT = 3        # before escalating to SIGQUIT
SHELL_KILL_SIGQUIT_WAIT = 2       # before escalating to respawn

HEARTBEAT_THRESHOLD_SECONDS = 10  # don't tick on fast tools (builder/planner default)
HEARTBEAT_INTERVAL_SECONDS = 20   # tick cadence after threshold (builder/planner default)

MODEL_RETRY_MAX_ATTEMPTS = 3
MODEL_RETRY_BASE_DELAY = 2        # seconds; doubled per attempt → 2, 4, 8
MODEL_RETRY_RETRYABLE_STATUS = {500, 502, 503, 504, 529}
STREAM_CHUNK_TIMEOUT_SECONDS = 60 # per-chunk; catches stuck streams faster than langchain's 120s

CHECKPOINT_SCHEMA_VERSION = 2     # bump when State/BuilderState TypedDict changes
RESUME_FRESHNESS_HOURS = 24       # checkpoints older than this aren't offered

VERIFY_COMPLETION_CAP = 3         # advisor verdicts per task before forced give_up
VERIFY_COMPLETION_ERROR_CAP = 2   # advisor errors (separate budget; doesn't burn verdict cap)
SHELL_HISTORY_FOR_VERIFY = 10     # ring buffer of recent shell outputs surfaced to advisor

FILE_VIEW_DEFAULT_MAX_LINES = 800 # if file ≤ this, return whole file (most source files <800)
FILE_VIEW_TRUNCATE_TO = 400       # if file >, return first N lines unless start/end specified

STALE_PLAN_HOURS = 24             # persisted plans older than this are advisory only
```

Tune from real validation runs (the trace logs are designed for this).

## Status

Experimental research harness. The PBE architecture and the sophisticated builder are intended to surface model failure modes rather than hide them — fabrication, broken tool-call formats, debug-loop thrashing, and abandonment all have explicit signals in the traces.
