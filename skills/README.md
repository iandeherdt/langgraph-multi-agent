# skills/

System prompts for the harness's three roles, kept as markdown files instead of triple-quoted Python literals.

## Convention

```
skills/<name>/SKILL.md
```

Anthropic Skills convention — directory per skill. The directory exists so a skill can grow supporting material (`examples/`, `templates/`, additional reference docs) without changing the loader.

## How they're loaded

`graph.py` reads each file at module import via `_load_skill(name)`:

```python
PLANNER_PROMPT          = _load_skill("planning")
BUILDER_BASE_SYSTEM_PROMPT = _load_skill("building")
EVALUATOR_SYSTEM_PROMPT = _load_skill("evaluating")
```

Read once. Editing a `SKILL.md` requires a fresh `docker compose run` to pick up — no rebuild (the project is bind-mounted at `/app:ro`), no hot-reload (intentional — file watching adds complexity for no real benefit at this scale).

## What's here

| Skill | Loaded as | Used by |
|---|---|---|
| `planning/SKILL.md` | `PLANNER_PROMPT` | `planner_node` (Claude Sonnet) |
| `building/SKILL.md` | `BUILDER_BASE_SYSTEM_PROMPT` | builder StateGraph (Qwen3-Coder-Next), prepended every turn alongside plan + step budget |
| `evaluating/SKILL.md` | `EVALUATOR_SYSTEM_PROMPT` | `evaluator_node` (Qwen3.6-27B + Playwright MCP) |

## No frontmatter

Anthropic's external Skills use YAML frontmatter (`name:`, `description:`) so an agent can discover and load skills at runtime. Ours are hardcoded by name in `graph.py`, so frontmatter is dead weight. Plain markdown.

If we ever build a skill-discovery layer, add frontmatter then.

## Editing rules

- Treat these as the source of truth for prompt content. Don't add prompt content back into `graph.py` as inline strings.
- Diffs to skill files should be reviewable as prose changes — that's the whole point of this layout.
- The Python harness handles structure (parsing the model's output, routing, persistence). The skill files handle voice and content (what to tell the model, in what shape).
