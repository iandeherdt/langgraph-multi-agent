# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A research harness for validating open-weight LLMs as agentic coding agents (target: AMD R9700 deployment). Architecture is a planner → builder → evaluator outer graph with a sophisticated custom builder StateGraph inside, plus an advisor (`verify_completion`) gating builder completion. See `README.md` for the user-facing overview; this file is for working *on* the harness.

## File map

The whole agent lives in `graph.py` (~6200 lines, single file by design — splitting would obscure the data flow). It's grown well past the original sketch as features land; the source is the canonical map. To navigate:

- **Top-of-file constants block** (~line 60–340) — every tunable knob, env var read, and feature flag. Tune here, don't hunt for magic numbers downstream. Sections, in order: workspace paths, run-summary paths, resume schema, MCP recovery, eval skip, eval evidence enforcement, eval idle thresholds, eval-incomplete patterns, MCP transport patterns, shell config + noninteractive env, heartbeat, model retry + stream timeout, checkpoints + resume, advisor, tiered evaluator, file editor, syntax check, stuck detector, server port wait, plan persistence, architecture sub-sections, **test gate**.
- **`TraceLogger` + `TRACE` singleton** — JSONL trace per task at `workspace/.trace/<UTC>-<slug>.jsonl`.
- **Per-run state machinery** — git checkpointing (`_init_workspace_git_repo`, `_create_checkpoint`), cost tracking (`_record_cost`, `_extract_usage`), iteration history + RUN_SUMMARY rendering, resume helpers (`_serialize_state_for_resume`, `_load_run_state`, `_validate_resume`, `_restore_module_state_from_resume`), prompt loaders (`--prompt-file` / `--prompt-name` / `--prompt -` / triple-quote interactive), test gate (`_run_test_gate`).
- **Helpers** — `_resolve` (path traversal guard), `_truncate_head_tail` (smart shell-output truncation), `_dedupe_repeated_string` (recover from AIMessageChunk's response_metadata concatenation), `_tool_msg_content_str` (multimodal-content normalization).
- **`PersistentShell`** — `pexpect`-based long-lived bash. Sentinel-pattern command/exit detection. Module-level singleton via `_get_shell()`.
- **Tools** — grouped: shell (`shell`, `shell_reset`, `run_shell_oneshot`), file editor (`view_file`, `str_replace`, `create_file`, `list_dir`), server lifecycle (`serve_in_background`, `stop_servers`), plan management (`view_plan`, `update_plan_item`, `add_plan_item`, `view_architecture`, `propose_architecture_change`), advisor (`verify_completion`), exit signals (`mark_done`, `request_user_help`, `give_up`, `revise_plan`).
- **Stuck detector** — `_check_stuck(state)` returns an injection message or None. Three signals: edit churn, build-error stagnation, tool repetition.
- **LLMs** — `planner_llm` + `advisor_llm` use ChatAnthropic. `builder_llm` + `evaluator_llm` (legacy/cheap-tier default) use OpenRouter. `_make_llm_for_model(slug)` picks the wrapper at runtime for the tier-selected evaluator. `_openrouter_llm` adds `extra_body.provider.require_parameters` and optional `OPENROUTER_PROVIDERS` pinning.
- **Skill-file system prompts** — loaded from `skills/<role>/SKILL.md` at module import (planning, building, evaluating, verifying). `_render_builder_system` augments per-turn with plan, step budget, and `RUN_SUMMARY.md` (read as `# PREVIOUS RUN STATE`).
- **Outer `State` and inner `BuilderState`** — separate TypedDicts.
- **Builder graph** — `builder_model_node` → `builder_tools_node` with explicit routers. Exit signals land in `_exit_holder`.
- **`builder_node`** — outer-graph wrapper that initializes `BuilderState`, runs the inner graph, and finalizes the iteration via `_finalize_iteration_summary` on early-exits.
- **`planner_node`** — Anthropic call; parses `# DECISION`, `# PROPOSAL_REVIEW`, `# REQUIREMENTS`, `# ARCHITECTURE`, `# TASKS`, `# BUILDER_INSTRUCTIONS`, `# EVALUATOR_INSTRUCTIONS`. Already-complete short-circuit lives here.
- **Evaluator** — `langchain.agents.create_agent` (V1). Tier selector (`_select_evaluator_model`) picks the LLM per-call; `build_evaluator_subagent(llm)` constructs the agent + persistent MCP session. MCP recovery + fallback-model retry wrap `_stream_subagent`. Verdict-validation retry tracks (empty notes, insufficient evidence) sit between verdict and `_create_checkpoint`. Test gate runs between verdict and checkpoint.
- **Outer routers + graph** — `route_after_planner` (already-complete short-circuit), `route_after_builder` (help/give_up/model_unreachable → END; replan/advisor_disagreement → planner; await_evaluator/done/etc → evaluator), `route_after_eval` (done → END; replan → planner; incomplete → END with diagnostic split between MCP-infra vs eval-communication-failure; otherwise → builder).
- **`main()`** — async REPL with arg parsing for `--no-checkpoint`, `--resume <id>`, `--prompt-file`, `--prompt -`, `--prompt-name`. Resume / fresh-run paths diverge before the loop; one-shot vs REPL after.

## Module-level holders (intentional)

LangChain `@tool` functions can't easily access LangGraph state. Module-level holders bridge the gap:

- `_plan_holder` — plan tools read/mutate the v2 plan doc here. Builder graph's `tools_node` calls `_set_plan_context(...)` before tool dispatch.
- `_exit_holder` — exit tools (`mark_done`, `request_user_help`, `give_up`, `revise_plan`) set the signal here. Builder routers check it after each model + tool node.
- `_verification_holder` — `verify_completion` cap counters + the issued single-use token `mark_done` consumes.
- `_files_touched_holder` — `str_replace` + `create_file` add their paths here; evaluator_node reads via `state["iteration_files_touched"]` for the eval-skip-on-no-UI-change check.
- `_evaluator_holder` — cached agent (rebuilt only when chosen model changes), persistent MCP session, last tier-selector decision (for RUN_SUMMARY rendering).
- `_eval_tool_history` — ring buffer of recent eval tool calls + bodies; recursion-limit + empty-notes-cap salvage extracts findings from this.
- `_shell_output_history` — recent shell outputs surfaced to the advisor in `verify_completion`.
- `_cost_tracker` — per-model cumulative usage + USD totals.
- `_iteration_history` — per-iteration entries rendered into RUN_SUMMARY's history section.
- `_iteration_summary_holder` — `verify_completion` captures the builder's `task_summary` here so the iteration-history line has something informative.
- `_git_checkpoint_state` — branch, last commit, commit count.
- `_test_gate_state` — disabled_baseline flag, failure streak, last status/duration/output.
- `_run_started_holder` — the originating task, captured once at task start so it survives across iterations + resumes.
- `_graph_holder` — saver-equipped builder + outer graphs (replaced at `main()` startup).

These are not thread-safe. Single-threaded async only.

## Sandbox / boundaries

- Everything runs inside the `langgraph` docker container. Workspace is `/workspace` (host bind mount `./workspace`). Project files (`/app`) are mounted read-only — the agent cannot corrupt its own source.
- File tools enforce path containment via `_resolve()`.
- Persistent shell is bash inside the same container; can `cd` anywhere reachable but writes only land where mounts allow.
- Playwright MCP is a sibling container at `playwright-mcp:8931`. `workspace/.playwright-mcp/` (host) is bind-mounted from the MCP's `/tmp/.playwright-mcp/` so screenshots/snapshots/console-logs are inspectable from the Mac without `docker cp`.

## Common commands

- `docker compose build` — rebuild the langgraph image (after `requirements.txt` or `Dockerfile` changes).
- `docker compose build playwright-mcp` — rebuild the MCP image (after `Dockerfile.playwright-mcp` changes; e.g., bumping `@playwright/mcp` version).
- `./run.sh` — interactive task REPL. Wraps `docker compose run --rm --use-aliases --service-ports langgraph python graph.py "$@"` plus a preflight that auto-removes stale port-3000-binding run containers.
- `./run.sh --resume <run-id>` — continue an interrupted run from `state.json`.
- `./run.sh --prompt-file <path>` / `--prompt-name <name>` / `--prompt -` — non-interactive prompt input modes (run one task and exit).
- `./shell.sh` — bare shell inside the container with the right flags. `docker compose exec langgraph bash` does NOT work (exec only attaches to `compose up` containers; the harness uses `compose run --rm`).
- `docker compose --profile local up -d llama-cpp` — start the local llama.cpp service (requires GGUF in `./models/`).
- `ls workspace/.trace/` — list trace files. `jq -c 'select(.kind == "stuck_fire")' workspace/.trace/*.jsonl` to grep specific events.
- `cat workspace/.harness/RUN_SUMMARY.md` — human-readable per-run summary.
- `git -C workspace log harness-run-<UTC> --oneline` — review per-iteration commits.

## Editing the harness

- **Adjusting thresholds**: top of `graph.py`. Restart, no rebuild.
- **Adding a tool**: write the `@tool` function, add to `_builder_tools()` list, update `skills/building/SKILL.md` to mention it.
- **Changing models**: edit `.env`. No code changes for env-overridable slugs (`PLANNER_MODEL`, `ADVISOR_MODEL`, `BUILDER_MODEL`, `EVAL_MODEL`, `HARNESS_EVALUATOR_MODEL`, `HARNESS_EVALUATOR_STRONG_MODEL`).
- **Changing exit semantics**: exit tools just write to `_exit_holder`. Routers in `after_model_router` / `after_tools_router` read it. Add a new exit type by adding a new tool + handling in `_format_builder_summary` + `route_after_builder`.
- **Bumping `RESUME_STATE_SCHEMA_VERSION`**: required whenever you add/remove fields in `_serialize_state_for_resume`. Add a `_SCHEMA_CHANGELOG[N]` entry naming the change so the schema-mismatch error is informative for resumed runs.
- **Bumping `CHECKPOINT_SCHEMA_VERSION`**: required whenever the `State` or `BuilderState` TypedDict shape changes. Old AsyncSqliteSaver checkpoints get rejected with `checkpoint_schema_mismatch`.
- **Editing skill files** (`skills/<role>/SKILL.md`): hot-reloaded on next harness invocation; no code changes needed.

## Trace log usage

Every event has `{ts, iter, step, kind, ...fields}`. Useful queries (with `jq`):

- Per-task tool-call counts: `jq -c 'select(.kind == "tool_call")' <file> | jq -s 'group_by(.tool) | map({tool: .[0].tool, n: length})'`
- All stuck-detector firings: `jq -c 'select(.kind == "stuck_fire")' <file>`
- Builder exit reasons across runs: `jq -r 'select(.kind == "builder_exit") | .reason' workspace/.trace/*.jsonl | sort | uniq -c`
- Verdict + tier distribution: `jq -c 'select(.kind == "evaluator_model_chosen")' workspace/.trace/*.jsonl | jq -s 'group_by(.tier_used) | map({tier: .[0].tier_used, n: length})'`
- Cost per model across runs: `jq -c 'select(.kind == "model_call_cost")' workspace/.trace/*.jsonl | jq -s 'group_by(.model) | map({model: .[0].model, total_usd: (map(.cost_usd) | add)})'`
- MCP transport recovery rate: `jq -c 'select(.kind == "mcp_transport_recovered" or .kind == "mcp_recovery_exhausted")' workspace/.trace/*.jsonl | jq -s 'group_by(.kind) | map({kind: .[0].kind, n: length})'`

## Don't

- Don't add `write_file` back to the builder. Full-file overwrites were the original pathology — `str_replace` is the primary edit tool.
- Don't add `create_react_agent` for the builder. The custom StateGraph exists specifically because we need step budget visibility, stuck detection, and explicit exit gates — none of which fit cleanly into the prebuilt ReAct loop. (The evaluator IS a `create_agent` because its budget + verdict-format are simpler.)
- Don't put TS/TSX in `SYNTAX_CHECK_EXTENSIONS` — single-file `tsc` is not useful. If we need it, it should be a debounced project-wide check, not per-edit.
- Don't bypass the persistent MCP session by reverting evaluator MCP loading to `client.get_tools()`. That mode opens a fresh session per call → fresh Playwright page per call → page state lost between calls (`browser_navigate` succeeds, then `browser_snapshot` returns `about:blank`).
- Don't downgrade the advisor (`ADVISOR_MODEL`) to Haiku to save cost. The advisor is the load-bearing second-opinion check on builder claims; cheaping out there defeats the harness's main resilience layer. Tier the **evaluator** instead via `HARNESS_EVALUATOR_TIER` if you need to control eval cost.
- Don't add `write_file`-style operations to the test gate. The gate is read-only — it runs `HARNESS_TEST_COMMAND` and reads exit code + output. Anything that mutates state belongs in the builder, gated by the verification token flow.
