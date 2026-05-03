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

- **Persistent bash session** via `pexpect` — `cd`, `export`, venv activations all survive across `shell()` calls.
- **Patch-based file editor**: `view_file` (line-numbered), `str_replace` (unique-match required), `create_file` (errors if exists). No `write_file` — full-file overwrites were the worst pathology in the previous design.
- **Structured plan in state** with `view_plan` / `update_plan_item` / `add_plan_item` tools. Plan re-renders into the system message every turn — never gets crowded out of context.
- **Step budget** rendered into every model turn (`Step 14 of 50, 36 tool calls remaining`), escalating to BUDGET WARNING and FINAL STEP.
- **Verification gate**: builder cannot exit by trailing off — must call `mark_done(verify_command, claim)` which actually runs the verify command and only exits on exit code 0. Two other clean exits: `request_user_help`, `give_up`.
- **Stuck detector** — three heuristics (edit churn, build-error stagnation, tool repetition) with thresholds named at the top of `graph.py`.
- **Per-edit syntax check** for `.py` (`py_compile`) and `.js/.cjs/.mjs` (`node --check`). TS/TSX deferred — single-file checks aren't meaningful for cross-file imports.
- **Smart truncation** (head + tail with byte-elision marker) on shell output.

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
| `OPENROUTER_PROVIDERS` | Comma-separated provider pin | (unset) |
| `PLAYWRIGHT_MCP_URL` | MCP server SSE URL | `http://playwright-mcp:8931/sse` |

OpenRouter routes flakily for tool-calling on some providers (the model returns native XML format, the provider doesn't translate it back). If you see broken tool calls, find a working provider in your OpenRouter activity log and pin via `OPENROUTER_PROVIDERS=...`.

The `local` compose profile starts a llama.cpp server alongside, for the eventual move off OpenRouter. See `docker-compose.yml`.

## Tunable thresholds

All of these are named constants at the top of `graph.py`:

```python
MAX_PBE_ITERATIONS = 5
MAX_BUILDER_STEPS = 50
BUILDER_BUDGET_WARNING_THRESHOLD = 10

STUCK_EDIT_REPEAT_THRESHOLD = 3
STUCK_EDIT_WINDOW = 10
STUCK_BUILD_ERROR_REPEAT = 2
STUCK_BUILD_HISTORY = 3
STUCK_TOOL_REPEAT = 2
STUCK_INJECTION_CAP = 3

SHELL_OUTPUT_HEAD_BYTES = 2000
SHELL_OUTPUT_TAIL_BYTES = 5000
```

Tune from real validation runs (the trace logs are designed for this).

## Status

Experimental research harness. The PBE architecture and the sophisticated builder are intended to surface model failure modes rather than hide them — fabrication, broken tool-call formats, debug-loop thrashing, and abandonment all have explicit signals in the traces.
