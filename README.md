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

- **Planner** — Claude Sonnet 4.6 (Anthropic API). Writes a structured plan and the explicit prompts the builder and evaluator receive. The other models don't get generic system prompts; they get prose authored by Sonnet for this specific iteration.
- **Builder** — Qwen3-Coder-Next via OpenRouter. Custom `StateGraph` (not `create_react_agent`) with a sophisticated tool surface (see below).
- **Evaluator** — Qwen3.6-27B (vision-capable) via OpenRouter, with read-only file tools + Playwright MCP browser tools. Verifies builder output via both code (`npm run build` etc.) and screenshots.

**Inner (builder StateGraph):**

```
START → model → tools → [router] → model | END
```

- **Persistent bash session** via `pexpect` — `cd`, `export`, venv activations all survive across `shell()` calls. Spawned with a noninteractive env (`CI=true`, `npm_config_yes=true`, `NEXT_TELEMETRY_DISABLED=1`, etc.) so npm/npx/prisma never reach a prompt. Timeouts trigger an escalating kill (SIGINT → SIGQUIT → respawn) and unconditional reset, so the next call always sees a clean session.
- **Patch-based file editor**: `view_file` (line-numbered), `str_replace` (unique-match required), `create_file` (errors if exists). No `write_file` — full-file overwrites were the worst pathology in the previous design.
- **Structured plan in state** (v2 schema): requirements + architecture (`stack` / `file_tree` / `data_model` / `key_decisions`, or `summary` for non-coding tasks) + tasks. Plan re-renders into the system message every turn. Builder mutates tasks via `update_plan_item` / `add_plan_item` and can flag architecture changes for planner review via `propose_architecture_change` (queues to a `pending_proposals` list — planner accepts/rejects on next iteration). Capped at `MAX_REPLANS=2` builder-triggered replans per task.
- **Step budget** rendered into every model turn (`Step 14 of 50, 36 tool calls remaining`), escalating to BUDGET WARNING and FINAL STEP.
- **Verification gate**: builder cannot exit by trailing off — must call `mark_done(verify_command, claim)` which actually runs the verify command and only exits on exit code 0. Two other clean exits: `request_user_help`, `give_up`.
- **Stuck detector** — three heuristics (edit churn, build-error stagnation, tool repetition) with thresholds named at the top of `graph.py`.
- **Per-edit syntax check** for `.py` (`py_compile`) and `.js/.cjs/.mjs` (`node --check`). TS/TSX deferred — single-file checks aren't meaningful for cross-file imports.
- **Smart truncation** (head + tail with byte-elision marker) on shell output.
- **Live progress** while things are running: planner and builder token-stream their output to stdout under `[planner]` / `[builder]` prefixes; long-running tools emit heartbeat ticks every 20s (`·· shell [40s, 4096B, +1024, last: "Downloading next@14..."]`), with `STUCK` flagged when stdout-bytes don't grow between ticks. All ticks also land in the trace as `tool_progress` events.

**Trace logging:** every tool call, tool result, state transition, stuck-detector firing, plan update, and exit reason is written as one line of JSONL to `workspace/.trace/<UTC>-<slug>.jsonl`. Without this you can't tell if harness changes actually helped.

## Quick start

```bash
cp .env.example .env
# Fill in ANTHROPIC_API_KEY and OPENAI_API_KEY (OpenRouter)

docker compose build
docker compose run --rm langgraph
```

Then at the `Task:` prompt, give it a coding task. Each task runs the full PBE loop (max 5 iterations) and emits a trace file you can grep.

## Configuration

All in `.env`:

| Var | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Sonnet planner | (required) |
| `PLANNER_MODEL` | Anthropic model ID | `claude-sonnet-4-6` |
| `OPENAI_API_KEY` | OpenRouter | (required) |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | `https://openrouter.ai/api/v1` |
| `BUILDER_MODEL` | Builder slug | `qwen/qwen3-coder-next` |
| `EVAL_MODEL` | Evaluator slug (must be vision-capable) | `qwen/qwen3.6-27b` |
| `OPENROUTER_PROVIDERS` | Comma-separated provider pin (priority order, fallbacks disabled) | (unset) |
| `OPENROUTER_IGNORE_PROVIDERS` | Comma-separated providers to exclude (other providers still tried) | (unset) |
| `PLAYWRIGHT_MCP_URL` | MCP server SSE URL | `http://playwright-mcp:8931/sse` |

OpenRouter routes flakily for tool-calling on some providers (the model returns native XML format, the provider doesn't translate it back). If you see broken tool calls, find a working provider in your OpenRouter activity log and pin via `OPENROUTER_PROVIDERS=...`. To exclude a single bad provider without pinning everything, use `OPENROUTER_IGNORE_PROVIDERS=parasail` (etc.) — the rest of the fallback set still runs.

The `local` compose profile starts a llama.cpp server alongside, for the eventual move off OpenRouter. See `docker-compose.yml`.

## Resilience

- **Model retry**: every planner / builder model call is wrapped in `_ainvoke_streaming`, which retries the full astream on transient upstream errors (HTTP 5xx, connection drops, asyncio timeouts) up to `MODEL_RETRY_MAX_ATTEMPTS=3` with exponential backoff (2s / 4s / 8s). Partial chunks from failed attempts are discarded; the returned `AIMessage` is always assembled from a single successful stream. Each retry shows `↻ <label> retry N/M in Ks (ErrorType: ...)` on stdout and a `model_retry` event in the trace. 4xx errors and 429 are NOT retried (TODO: 429 needs Retry-After parsing).
- **Checkpoint resume**: outer + inner graph state is persisted to `workspace/.trace/checkpoints.db` (SQLite) at every node boundary. On crash, the next start scans for unfinished tasks (last trace event isn't `task_end`, file modified <24h ago) and prompts `Resume? [y/N]` (default N — fresh runs are fresh by default; same-task-text does NOT auto-resume). On resume, the inner builder graph picks up at the exact step it crashed at, not from step 0.
- **Schema versioning**: each saved checkpoint is stamped with `CHECKPOINT_SCHEMA_VERSION` in metadata. **Bump it whenever the `State` or `BuilderState` TypedDict shape changes** (in `graph.py`). Mismatched checkpoints are rejected with a `checkpoint_schema_mismatch` trace event and the run starts fresh — no silent corruption from old state.

### Manual checkpoint-resume verification

There's no automated test for end-to-end resume (it requires a real model + a kill mid-builder), but the recipe is short:

1. `docker compose run --rm langgraph` and give it a multi-step task that takes ≥30s of builder work (e.g. "scaffold a Next.js app with Prisma").
2. Watch the builder progress (`Step N of 50`). When you see, say, step 5 has executed, **kill the process** (Ctrl-C the docker run, or `docker kill` from another shell).
3. Confirm `workspace/.trace/checkpoints.db` exists and the most-recent trace `.jsonl` does NOT end with a `task_end` line: `tail -1 workspace/.trace/<latest>.jsonl | jq .kind` → not `"task_end"`.
4. `docker compose run --rm langgraph` again. Expect: `Found unfinished task from Nm ago: '<task text>' / Last event: <kind> / Resume? [y/N]:`. Type `y`.
5. Verify in stdout that the builder step counter resumes at ≥6 (the exact post-crash checkpoint), not 1. The trace will show a `task_resume` event followed by the existing iteration's events continuing.

If step 5 shows the counter at 1, checkpointing isn't actually working — check that `_graph_holder["builder"]` is being replaced in `main()`.

For automated resilience tests (model retry classification, schema mismatch detection, transient-error retry), run:

```bash
docker compose run --rm langgraph python /app/test_resilience.py
```

## Tunable thresholds

All of these are named constants at the top of `graph.py`:

```python
MAX_PBE_ITERATIONS = 5
MAX_BUILDER_STEPS = 50
BUILDER_BUDGET_WARNING_THRESHOLD = 10
MAX_REPLANS = 2

STUCK_EDIT_REPEAT_THRESHOLD = 3
STUCK_EDIT_WINDOW = 10
STUCK_BUILD_ERROR_REPEAT = 2
STUCK_BUILD_HISTORY = 3
STUCK_TOOL_REPEAT = 2
STUCK_INJECTION_CAP = 3

SHELL_COMMAND_TIMEOUT_SECONDS = 300
SHELL_OUTPUT_HEAD_BYTES = 2000
SHELL_OUTPUT_TAIL_BYTES = 5000
SHELL_KILL_SIGINT_WAIT = 3      # before escalating to SIGQUIT
SHELL_KILL_SIGQUIT_WAIT = 2     # before escalating to respawn

HEARTBEAT_THRESHOLD_SECONDS = 10  # don't tick on fast tools
HEARTBEAT_INTERVAL_SECONDS = 20   # tick cadence after threshold

MODEL_RETRY_MAX_ATTEMPTS = 3
MODEL_RETRY_BASE_DELAY = 2        # seconds; doubled per attempt → 2, 4, 8
MODEL_RETRY_RETRYABLE_STATUS = {500, 502, 503, 504, 529}

CHECKPOINT_SCHEMA_VERSION = 1     # bump when State/BuilderState TypedDict changes
RESUME_FRESHNESS_HOURS = 24       # checkpoints older than this aren't offered
```

Tune from real validation runs (the trace logs are designed for this).

## Status

Experimental research harness. The PBE architecture and the sophisticated builder are intended to surface model failure modes rather than hide them — fabrication, broken tool-call formats, debug-loop thrashing, and abandonment all have explicit signals in the traces.
