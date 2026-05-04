# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A research harness for validating open-weight LLMs as agentic coding agents (target: AMD R9700 deployment). Architecture is a planner-builder-evaluator outer graph with a sophisticated custom builder StateGraph inside. See `README.md` for the user-facing overview; this file is for working *on* the harness.

## File map

The whole agent lives in `graph.py` (~900 lines, single file by design — splitting would obscure the data flow). Sections, in order:

1. **Constants** — every tunable knob (loop caps, stuck-detector thresholds, truncation byte budgets, syntax-check extensions). Tune here, don't hunt for magic numbers downstream.
2. **`TraceLogger` + `TRACE` singleton** — JSONL trace per task at `workspace/.trace/`. Every event flows through `TRACE.log(kind, **fields)`.
3. **Helpers** — `_resolve` (path traversal guard), `_truncate_head_tail` (smart shell-output truncation), `_parse_plan` / `_render_plan` (plan markdown ↔ structured items).
4. **`PersistentShell`** — `pexpect`-based long-lived bash. Sentinel-pattern command/exit detection. Module-level singleton via `_get_shell()`.
5. **Tools** — grouped: shell (`shell`, `shell_reset`, `run_shell_oneshot`), file editor (`view_file`, `str_replace`, `create_file`, `list_dir`), server lifecycle (`serve_in_background`, `stop_servers`), plan management (`view_plan`, `update_plan_item`, `add_plan_item`), exit signals (`mark_done`, `request_user_help`, `give_up`).
6. **Stuck detector** — `_check_stuck(state)` returns an injection message or None. Three signals: edit churn, build-error stagnation, tool repetition.
7. **LLMs** — Anthropic for planner, OpenRouter for builder/evaluator. `_openrouter_llm` adds `extra_body.provider.require_parameters` and optional pinning.
8. **System prompts** — `PLANNER_PROMPT`, `BUILDER_BASE_SYSTEM_PROMPT`, `EVALUATOR_SYSTEM_PROMPT`. Builder prompt is augmented per-turn with plan + step budget by `_render_builder_system`.
9. **Outer `State` and inner `BuilderState`** — separate TypedDicts.
10. **Builder graph** — `builder_model_node` → `builder_tools_node` with explicit routers. Exit signals land in module-level `_exit_holder`.
11. **`builder_node`** — outer-graph wrapper that initializes `BuilderState` and runs the builder graph.
12. **`planner_node`** — Anthropic call; parses three markdown sections (`# PLAN`, `# BUILDER_INSTRUCTIONS`, `# EVALUATOR_INSTRUCTIONS`).
13. **Evaluator** — uses `langchain.agents.create_agent` (V1) with try/except fallback to `langgraph.prebuilt.create_react_agent`. MCP tools loaded async on first eval invocation.
14. **Outer routers + graph** — `route_after_builder` ends on `help`/`give_up`, otherwise hands to evaluator. `route_after_eval` ends on `done` or iteration cap.
15. **`main()`** — async REPL, one task per `input()` line.

## Module-level holders (intentional)

LangChain `@tool` functions can't easily access LangGraph state. Two module-level holders bridge the gap:

- `_plan_holder["items"]` — plan tools read/mutate this. The builder graph's `tools_node` calls `_set_plan(state["plan"])` before tool dispatch and reads back via `_get_plan()` after.
- `_exit_holder["signal"], _exit_holder["payload"]` — exit tools (`mark_done`, `request_user_help`, `give_up`) set this. The builder routers check it after each model and tool node.

These are not thread-safe. Single-threaded async only.

## Sandbox / boundaries

- Everything runs inside the `langgraph` docker container. Workspace is `/workspace` (host bind mount `./workspace`). Project files (`/app`) are mounted read-only — the agent cannot corrupt its own source.
- File tools enforce path containment via `_resolve()`.
- Persistent shell is bash inside the same container; can `cd` anywhere reachable but writes only land where mounts allow.
- Playwright MCP is a sibling container at `playwright-mcp:8931`.

## Common commands

- `docker compose build` — rebuild the langgraph image (after `requirements.txt` or `Dockerfile` changes).
- `./run.sh` (or `docker compose run --rm --use-aliases --service-ports langgraph`) — interactive task REPL. Two flags needed:
  - `--use-aliases`: without it, the transient run container only registers its container-name as a network alias, so the playwright-mcp sibling can't resolve `langgraph:3000` for `browser_navigate` → Firefox returns `NS_ERROR_UNKNOWN_HOST`.
  - `--service-ports`: `compose run` ignores the service's `ports:` spec by default (a known compose-run-vs-up difference). Without this, the dev server the builder spawns is reachable inside the container and from playwright-mcp, but NOT from the host browser. With it, `http://localhost:3000` on the host works.
- `./run.sh bash` (or `docker compose run --rm --use-aliases --service-ports langgraph bash`) — shell inside the container for debugging. Same flags apply.
- `docker compose --profile local up -d llama-cpp` — start the local llama.cpp service (requires GGUF in `./models/`).
- `ls workspace/.trace/` — list trace files. `jq -c 'select(.kind == "stuck_fire")' workspace/.trace/*.jsonl` to grep specific events.

## Editing the harness

- **Adjusting thresholds**: top of `graph.py`. Restart, no rebuild.
- **Adding a tool**: write the `@tool` function, add to `_builder_tools()` list, update `BUILDER_BASE_SYSTEM_PROMPT` to mention it.
- **Changing models**: edit `.env`. No code changes; the `_openrouter_llm` helper picks them up.
- **Changing exit semantics**: the three exit tools just write to `_exit_holder`. Routers in `after_model_router` / `after_tools_router` read it. Add a new exit type by adding a new tool + handling in `_format_builder_summary` + `route_after_builder`.

## Trace log usage

Every event has `{ts, iter, step, kind, ...fields}`. Useful queries (with `jq`):

- Per-task tool-call counts: `jq -c 'select(.kind == "tool_call")' <file> | jq -s 'group_by(.tool) | map({tool: .[0].tool, n: length})'`
- All stuck-detector firings: `jq -c 'select(.kind == "stuck_fire")' <file>`
- Verify-failed cycles: `jq -c 'select(.kind == "verify_failed")' <file>`
- Builder exit reasons across runs: `jq -r 'select(.kind == "builder_exit") | .reason' workspace/.trace/*.jsonl | sort | uniq -c`

## Don't

- Don't add `write_file` back to the builder. Full-file overwrites were the original pathology.
- Don't add `create_react_agent` for the builder. The custom StateGraph exists specifically because we need step budget visibility, stuck detection, and explicit exit gates — none of which fit cleanly into the prebuilt ReAct loop.
- Don't put TS/TSX in `SYNTAX_CHECK_EXTENSIONS` — single-file `tsc` is not useful. If we need it, it should be a debounced project-wide check, not per-edit.
