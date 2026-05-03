"""Planner → Builder → Evaluator harness.

Outer graph (PBE):
    planner_node → builder_node → [router] → evaluator_node → [router] → planner_node | END

Builder is a custom StateGraph (NOT create_react_agent) with:
- patch-based file editor (view_file, str_replace, create_file)
- persistent pexpect bash session (shell, shell_reset)
- structured plan in state with view/update/add/revise tools
- explicit exit tools: mark_done (with verify gate), request_user_help, give_up
- visible step budget rendered into every model turn
- stuck detector (edit churn / build-error stagnation / tool repetition)
- per-edit syntax check for Python + JS (NOT TS — see comments)
- JSONL trace logging of every tool call, result, state transition

Evaluator stays as a ReAct-style agent (langchain.agents.create_agent), with
read-only file tools + Playwright MCP.
"""

import asyncio
import hashlib
import json
import os
import re
import socket
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from typing_extensions import TypedDict

import pexpect
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages

try:
    from langchain.agents import create_agent  # V1 (langchain>=1.0): kwarg is `system_prompt`
    _AGENT_PROMPT_KWARG = "system_prompt"
except ImportError:
    from langgraph.prebuilt import create_react_agent as create_agent  # legacy: kwarg is `prompt`
    _AGENT_PROMPT_KWARG = "prompt"


# ────────────────────────── constants ──────────────────────────

WORKSPACE = Path("/workspace")
TRACE_DIR = WORKSPACE / ".trace"

# Outer (PBE) loop cap
MAX_PBE_ITERATIONS = 5

# Builder loop budget (per PBE iteration)
MAX_BUILDER_STEPS = 50
BUILDER_BUDGET_WARNING_THRESHOLD = 10  # remaining ≤ this → "BUDGET WARNING"

# Evaluator loop cap
EVAL_RECURSION_LIMIT = 40

# Shell (both persistent and one-shot)
SHELL_COMMAND_TIMEOUT_SECONDS = 300
SHELL_OUTPUT_HEAD_BYTES = 2000        # head of head+tail truncation
SHELL_OUTPUT_TAIL_BYTES = 5000        # tail (bias toward exit-code/end-of-build errors)

# File editor
FILE_VIEW_DEFAULT_MAX_LINES = 400     # if file ≤ this, return whole file by default
FILE_VIEW_TRUNCATE_TO = 200           # if file > default, return first N lines unless start/end specified
FILE_READ_HARD_CAP_BYTES = 200_000    # absolute max bytes returnable from one view_file call

# Per-edit syntax check.
# TS/TSX intentionally excluded: tsc --noEmit on a single file either errors on every cross-file
# import or checks nothing useful. Rely on the verification gate instead. (If validation runs show
# we need it, add a debounced project-wide tsc --noEmit scheduled after N edits or M idle seconds.)
SYNTAX_CHECK_EXTENSIONS = {".py", ".js", ".cjs", ".mjs"}
SYNTAX_CHECK_TIMEOUT_SECONDS = 10

# Stuck detector — TUNE FROM VALIDATION RUNS (these are the knobs to twist)
STUCK_EDIT_REPEAT_THRESHOLD = 3       # same (file, edit-fingerprint) ≥ this → fire
STUCK_EDIT_WINDOW = 10                # within last N edits
STUCK_BUILD_ERROR_REPEAT = 2          # same build-error fingerprint in ≥ this many of last K builds
STUCK_BUILD_HISTORY = 3               # K = window for build-error comparison
STUCK_TOOL_REPEAT = 2                 # identical (tool, args) consecutively ≥ this → fire
STUCK_INJECTION_CAP = 3               # max stuck-injection messages before forced exit
NO_TOOL_CALL_REMINDER_CAP = 2         # consecutive no-tool-call turns before exit_signal=abandoned

# Background server
SERVER_PORT_LISTEN_TIMEOUT_SECONDS = 30

# Skill files (system prompts live in skills/<name>/SKILL.md, loaded at import).
SKILLS_DIR = Path(__file__).parent / "skills"

# Plan persistence
CURRENT_PLAN_PATH = TRACE_DIR / "current-plan.json"
CURRENT_PLAN_VERSION = 2              # v2: requirements + architecture + tasks (was: items)
MAX_REPLANS = 2                       # cap on builder-triggered revise_plan calls per task
STALE_PLAN_HOURS = 24                 # v1 — expected to change after usage data; bump as needed

# Architecture sub-section names (in canonical render order). "summary" is the non-coding-task
# variant — emitted alone, no other sub-sections. The renderer handles missing sub-sections.
ARCHITECTURE_SUBSECTIONS = ("stack", "file_tree", "data_model", "key_decisions")
NON_CODING_SUBSECTION = "summary"


# ────────────────────────── trace logger ──────────────────────────


class TraceLogger:
    """JSONL trace logger. One event per line, flushed immediately. One file per task run."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.fh = None
        self.path: Path | None = None
        self.iter = 0
        self.step = 0

    def start_task(self, task_text: str) -> Path:
        if self.fh:
            self.end_task(reason="abandoned_for_new_task")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = re.sub(r"[^a-z0-9]+", "-", task_text.lower())[:40].strip("-") or "task"
        self.path = self.base_dir / f"{ts}-{slug}.jsonl"
        self.fh = open(self.path, "w")
        self.iter = 0
        self.step = 0
        self.log("task_start", task=task_text)
        return self.path

    def end_task(self, **fields) -> None:
        if self.fh:
            try:
                self.log("task_end", **fields)
                self.fh.close()
            except Exception:
                pass
        self.fh = None
        self.path = None

    def set_iter(self, n: int) -> None:
        self.iter = n

    def set_step(self, n: int) -> None:
        self.step = n

    def log(self, kind: str, **fields) -> None:
        if not self.fh:
            return
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "iter": self.iter,
            "step": self.step,
            "kind": kind,
            **fields,
        }
        try:
            self.fh.write(json.dumps(event, default=str) + "\n")
            self.fh.flush()
        except Exception:
            pass  # never let logging crash the agent


TRACE = TraceLogger(TRACE_DIR)


# ────────────────────────── helpers ──────────────────────────


def _load_skill(name: str) -> str:
    """Load skills/<name>/SKILL.md verbatim. Read at module import; restart to reload.

    System prompts live as markdown files (diffable, editable as docs) instead of triple-quoted
    Python literals. The harness only loads them — content authority lives in the .md file.
    """
    p = SKILLS_DIR / name / "SKILL.md"
    if not p.exists():
        raise FileNotFoundError(f"Skill not found: {p}")
    return p.read_text()


def _resolve(path: str) -> Path:
    p = (WORKSPACE / path).resolve()
    if not (p == WORKSPACE or p.is_relative_to(WORKSPACE)):
        raise ValueError(f"path '{path}' escapes /workspace")
    return p


def _truncate_head_tail(s: str, head_bytes: int, tail_bytes: int) -> str:
    """Smart truncation: keep both head AND tail, with byte-elision marker between."""
    total_max = head_bytes + tail_bytes
    if len(s) <= total_max:
        return s
    elided = len(s) - head_bytes - tail_bytes
    return f"{s[:head_bytes]}\n\n[... {elided} bytes elided ...]\n\n{s[-tail_bytes:]}"


def _truncate_simple(s: str, n: int = 300) -> str:
    return s if len(s) <= n else s[:n] + f"...[+{len(s) - n} chars]"


def _format_args(args: dict) -> str:
    parts = []
    for k, v in args.items():
        s = repr(v)
        if len(s) > 200:
            s = s[:200] + f"...[+{len(s) - 200} chars]"
        parts.append(f"{k}={s}")
    return ", ".join(parts)


def _hash_short(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:12]


def _extract_section(text: str, name: str) -> str:
    """Extract content under a LEVEL-1 # header. Stops at the next level-1 header (not at ##)."""
    pattern = rf"^#\s+{re.escape(name)}\s*\n(.*?)(?=^#\s|\Z)"
    m = re.search(pattern, text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_verdict(text: str) -> str:
    m = re.search(r"VERDICT:\s*(done|continue|replan)", text, re.IGNORECASE)
    return m.group(1).lower() if m else "continue"


def _extract_notes(text: str) -> str:
    m = re.search(r"NOTES:\s*(.+?)(?=\n[A-Z_]+:\s|\Z)", text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _parse_tasks(plan_text: str) -> list[dict]:
    """Parse a markdown checklist into TaskItem dicts.

    Accepts: '- [ ] text', '- [x] text', '- text', '* text', '1. text'.
    Returns: [{id, text, status: 'todo'|'done', notes: ''}, ...]
    """
    items = []
    next_id = 1
    for raw in plan_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^[-*]\s+\[(.)\]\s+(.+)$", line)
        if m:
            mark = m.group(1).lower()
            text = m.group(2).strip()
            status = "done" if mark == "x" else "todo"
        else:
            m = re.match(r"^(?:[-*]|\d+\.)\s+(.+)$", line)
            if m:
                text = m.group(1).strip()
                status = "todo"
            else:
                continue
        items.append({"id": next_id, "text": text, "status": status, "notes": ""})
        next_id += 1
    return items


def _render_tasks(items: list[dict]) -> str:
    if not items:
        return "(no tasks)"
    out = []
    sym = {"todo": "[ ]", "doing": "[~]", "done": "[x]", "blocked": "[!]"}
    for it in items:
        marker = sym.get(it["status"], "[ ]")
        line = f"{it['id']}. {marker} {it['text']}"
        if it.get("notes"):
            line += f"  ({it['notes']})"
        out.append(line)
    return "\n".join(out)


def _parse_requirements(text: str) -> list[str]:
    """Parse a bullet list from REQUIREMENTS section text. Returns list of strings."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^[-*]\s+(.+)$", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _render_requirements(reqs: list[str]) -> str:
    if not reqs:
        return "(none)"
    return "\n".join(f"- {r}" for r in reqs)


_KNOWN_ARCH_SUBNAMES = "|".join(ARCHITECTURE_SUBSECTIONS + (NON_CODING_SUBSECTION,))


def _parse_architecture(text: str) -> dict[str, str]:
    """Parse ARCHITECTURE text into a sub-section dict.

    Recognises only the canonical sub-section names (stack/file_tree/data_model/key_decisions
    for coding tasks; summary for non-coding). Each sub-section value is the raw markdown
    content under that ## header, stripped. Missing sub-sections are simply absent from the dict.
    """
    if not text or not text.strip():
        return {}
    arch = {}
    pattern = re.compile(
        rf"^\s*##\s+({_KNOWN_ARCH_SUBNAMES})\s*\n(.*?)(?=^\s*##\s+(?:{_KNOWN_ARCH_SUBNAMES})\b|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        subname = m.group(1).strip().lower()
        content = m.group(2).strip()
        arch[subname] = content
    return arch


def _render_architecture(arch: dict[str, str]) -> str:
    """Render architecture dict back to markdown sub-sections in canonical order.

    Handles missing sub-sections gracefully: only emits what's present. Coding sub-sections
    in canonical order, then summary if present.
    """
    if not arch:
        return "(no architecture specified)"
    parts = []
    for name in ARCHITECTURE_SUBSECTIONS:
        if name in arch:
            parts.append(f"## {name}\n{arch[name]}")
    if NON_CODING_SUBSECTION in arch:
        parts.append(f"## {NON_CODING_SUBSECTION}\n{arch[NON_CODING_SUBSECTION]}")
    return "\n\n".join(parts) if parts else "(no architecture specified)"


def _render_proposals(proposals: list[dict]) -> str:
    """Render pending proposals as a numbered list (for builder context AND planner review input)."""
    if not proposals:
        return "(none pending)"
    out = []
    for i, p in enumerate(proposals, start=1):
        out.append(
            f'{i}. [section={p.get("section", "?")}] '
            f'change="{p.get("change", "?")}" '
            f'rationale="{p.get("rationale", "?")}"'
        )
    return "\n".join(out)


def _render_plan_doc(doc: dict) -> str:
    """Render the full plan document for the builder system message.

    Layout: # REQUIREMENTS / # ARCHITECTURE / # TASKS / # PENDING ARCHITECTURE PROPOSALS (if any).
    """
    parts = []
    parts.append("# REQUIREMENTS\n" + _render_requirements(doc.get("requirements", [])))
    parts.append("\n# ARCHITECTURE\n" + _render_architecture(doc.get("architecture", {})))
    parts.append("\n# TASKS\n" + _render_tasks(doc.get("tasks", [])))
    pending = doc.get("pending_proposals", [])
    if pending:
        parts.append(
            "\n# PENDING ARCHITECTURE PROPOSALS (awaiting planner review)\n"
            + _render_proposals(pending)
        )
    return "\n".join(parts)


def _parse_proposal_review(text: str) -> list[dict]:
    """Parse # PROPOSAL_REVIEW entries.

    Format: '1. accepted: rationale' or '1. rejected: rationale'. Returns list of
    {index, decision, rationale} dicts (1-based index). Missing entries are NOT filled
    here — caller detects skips by comparing to the proposal count.
    """
    if not text:
        return []
    entries = []
    pattern = re.compile(
        r"^\s*(\d+)\.\s+(accepted|rejected)\s*:\s*(.+?)"
        r"(?=^\s*\d+\.\s+(?:accepted|rejected)\s*:|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        entries.append({
            "index": int(m.group(1)),
            "decision": m.group(2).lower(),
            "rationale": m.group(3).strip(),
        })
    return entries


def _upconvert_v1_to_v2(payload: dict) -> dict:
    """Convert a v1 plan to v2 schema.

    v1 'items' becomes v2 'tasks'; requirements + architecture are empty placeholders.
    The planner sees `_upconverted_from: 1` on next entry and fills them in.
    """
    return {
        "version": 2,
        "task": payload.get("task", ""),
        "updated_at": payload.get("updated_at"),
        "trace_file": payload.get("trace_file"),
        "replan_count": payload.get("replan_count", 0),
        "requirements": [],
        "architecture": {},
        "tasks": payload.get("items", []),
        "pending_proposals": [],
        "_upconverted_from": 1,
    }


def _is_build_command(cmd: str) -> bool:
    """Heuristic — does this command produce build/compile errors worth fingerprinting?"""
    return bool(re.search(
        r"\b(npm run build|npm run test|npm test|tsc|next build|pnpm build|pnpm test|"
        r"yarn build|yarn test|cargo build|cargo test|pytest|go build|go test|make)\b",
        cmd,
    ))


def _build_error_fingerprint(output: str) -> str | None:
    """Hash the first error-shaped line (for stuck detection)."""
    for line in output.splitlines():
        if "error" in line.lower():
            return _hash_short(line.strip()[:200])
    return None


def _maybe_syntax_check(path: Path) -> str | None:
    """Run a single-file syntax check if the extension supports it.

    Returns None on pass/skip, error string on fail.
    """
    if path.suffix not in SYNTAX_CHECK_EXTENSIONS:
        return None
    try:
        if path.suffix == ".py":
            r = subprocess.run(
                ["python", "-m", "py_compile", str(path)],
                capture_output=True, text=True, timeout=SYNTAX_CHECK_TIMEOUT_SECONDS,
            )
        else:  # .js, .cjs, .mjs
            r = subprocess.run(
                ["node", "--check", str(path)],
                capture_output=True, text=True, timeout=SYNTAX_CHECK_TIMEOUT_SECONDS,
            )
    except Exception:
        return None  # never let check infra failures block edits
    if r.returncode == 0:
        return None
    return _truncate_simple((r.stderr or r.stdout or "").strip(), 1000)


# ────────────────────────── persistent shell ──────────────────────────


class PersistentShell:
    """Long-lived bash session via pexpect. cwd, env, venv all persist across commands."""

    PROMPT = "__SHELL_PROMPT_X1Y2Z3__"

    def __init__(self, cwd: Path = WORKSPACE):
        self.cwd = cwd
        self.proc: pexpect.spawn | None = None
        self._spawn()

    def _spawn(self) -> None:
        self.proc = pexpect.spawn(
            "/bin/bash",
            ["--norc", "--noprofile", "-i"],
            cwd=str(self.cwd),
            encoding="utf-8",
            echo=False,
            timeout=30,
        )
        self.proc.sendline("PROMPT_COMMAND=")
        self.proc.sendline(f"PS1='{self.PROMPT}\\n'")
        self.proc.sendline("set +o history")
        self.proc.sendline("set +m")  # silence job-control
        # Drain to first prompt
        try:
            self.proc.expect_exact(self.PROMPT, timeout=5)
        except pexpect.exceptions.ExceptionPexpect:
            pass

    def run(self, command: str, timeout: int = SHELL_COMMAND_TIMEOUT_SECONDS) -> dict:
        if not self.proc or not self.proc.isalive():
            self._spawn()
        sentinel = f"__EXIT_{int(time.time() * 1_000_000)}__"
        start = time.time()

        self.proc.sendline(command)
        self.proc.sendline(f"echo '{sentinel}'$?")

        timed_out = False
        try:
            self.proc.expect(rf"{re.escape(sentinel)}(\d+)", timeout=timeout)
            output = self.proc.before or ""
            exit_code = int(self.proc.match.group(1))
        except pexpect.TIMEOUT:
            timed_out = True
            output = self.proc.before or ""
            exit_code = -1
            # SIGINT and try to recover
            try:
                self.proc.sendcontrol("c")
                self.proc.expect(rf"{re.escape(sentinel)}(\d+)", timeout=10)
                output = self.proc.before or ""
                exit_code = int(self.proc.match.group(1))
            except pexpect.exceptions.ExceptionPexpect:
                self.reset()

        # Drain to next prompt sentinel
        try:
            self.proc.expect_exact(self.PROMPT, timeout=5)
        except pexpect.exceptions.ExceptionPexpect:
            pass

        return {
            "output": output.strip("\r\n"),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "elapsed_ms": int((time.time() - start) * 1000),
        }

    def reset(self) -> None:
        try:
            if self.proc and self.proc.isalive():
                self.proc.terminate(force=True)
        except Exception:
            pass
        self._spawn()


_shell_session: PersistentShell | None = None


def _get_shell() -> PersistentShell:
    global _shell_session
    if _shell_session is None:
        _shell_session = PersistentShell(WORKSPACE)
    return _shell_session


# ────────────────────────── tools: shell ──────────────────────────


@tool
def shell(command: str) -> str:
    """Run a bash command in the LONG-LIVED session. State (cwd, env, venv, history) PERSISTS.

    Use for git, npm, npx, python, etc. Pass --yes/-y to skip prompts.
    Stdin is closed; interactive programs (vim, REPLs) will hang and time out.

    300s timeout per command. Output is truncated head + tail with byte-elision marker.
    """
    sh = _get_shell()
    result = sh.run(command, timeout=SHELL_COMMAND_TIMEOUT_SECONDS)
    output = result["output"]
    exit_code = result["exit_code"]
    timed_out = result["timed_out"]

    truncated = _truncate_head_tail(output, SHELL_OUTPUT_HEAD_BYTES, SHELL_OUTPUT_TAIL_BYTES)
    suffix = (
        f"\n[TIMEOUT after {SHELL_COMMAND_TIMEOUT_SECONDS}s, sent SIGINT]"
        if timed_out else f"\n[exit code: {exit_code}]"
    )
    TRACE.log(
        "tool_result", tool="shell",
        ok=(exit_code == 0), exit_code=exit_code, timed_out=timed_out,
        elapsed_ms=result["elapsed_ms"], output_chars=len(output),
    )
    return truncated + suffix


@tool
def shell_reset() -> str:
    """Kill the persistent bash session and start a fresh one. Use when state is corrupted."""
    sh = _get_shell()
    sh.reset()
    TRACE.log("shell_reset")
    return "shell session reset"


@tool
def run_shell_oneshot(command: str) -> str:
    """One-shot bash command (no persistent state). For evaluator verification.

    Each invocation spawns a fresh bash. cwd resets to /workspace. 300s timeout.
    """
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(WORKSPACE),
            capture_output=True, text=True,
            timeout=SHELL_COMMAND_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "") + ((e.stderr or "") and f"\n[stderr]\n{e.stderr}")
        TRACE.log("tool_result", tool="run_shell_oneshot", ok=False, timed_out=True)
        return _truncate_head_tail(out, SHELL_OUTPUT_HEAD_BYTES, SHELL_OUTPUT_TAIL_BYTES) + \
            f"\n[TIMEOUT after {SHELL_COMMAND_TIMEOUT_SECONDS}s]"
    out = result.stdout
    if result.stderr:
        out += f"\n[stderr]\n{result.stderr}"
    out += f"\n[exit code: {result.returncode}]"
    TRACE.log(
        "tool_result", tool="run_shell_oneshot",
        ok=(result.returncode == 0), exit_code=result.returncode, output_chars=len(out),
    )
    return _truncate_head_tail(out, SHELL_OUTPUT_HEAD_BYTES, SHELL_OUTPUT_TAIL_BYTES)


# ────────────────────────── tools: file editor ──────────────────────────


@tool
def view_file(path: str, start: int = 1, end: int | None = None) -> str:
    """Read a file under /workspace, returning lines with 1-indexed line numbers prefixed.

    Default returns the whole file if ≤ 400 lines, else the first 200 with a note about
    how to view more. Pass start/end (1-indexed, inclusive) for explicit ranges.
    Hard cap: 200KB.
    """
    try:
        p = _resolve(path)
    except ValueError as e:
        TRACE.log("tool_result", tool="view_file", ok=False, error=str(e))
        return f"ERROR: {e}"
    if not p.exists():
        return f"ERROR: {path} does not exist"
    if not p.is_file():
        return f"ERROR: {path} is not a file"

    try:
        text = p.read_text()[:FILE_READ_HARD_CAP_BYTES]
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

    lines = text.splitlines()
    total = len(lines)

    if end is None:
        if total <= FILE_VIEW_DEFAULT_MAX_LINES:
            end = total
        else:
            end = min(start + FILE_VIEW_TRUNCATE_TO - 1, total)

    start = max(1, start)
    end = min(total, end if end is not None else total)
    if start > end:
        return f"ERROR: empty range (start={start} > end={end}, file has {total} lines)"

    width = len(str(end))
    body = "\n".join(f"{str(i).rjust(width)}: {lines[i - 1]}" for i in range(start, end + 1))
    header = (
        f"[showing lines {start}-{end} of {total}]\n"
        if (start > 1 or end < total) else f"[file: {total} lines total]\n"
    )
    TRACE.log(
        "tool_result", tool="view_file", ok=True,
        path=path, lines_shown=(end - start + 1), total_lines=total,
    )
    return header + body


@tool
def str_replace(path: str, old_str: str, new_str: str) -> str:
    """Replace a string in a file. old_str MUST match exactly once.

    On 0 matches: error. On >1 matches: error with count — widen old_str with surrounding context.
    On 1 match: replace, run a per-edit syntax check (.py / .js only), report.

    Use this for ALL existing-file edits. Prefer over recreating the file.
    """
    try:
        p = _resolve(path)
    except ValueError as e:
        TRACE.log("tool_result", tool="str_replace", ok=False, path=path, error=str(e))
        return f"ERROR: {e}"
    if not p.exists():
        return f"ERROR: {path} does not exist (use create_file for new files)"
    if not p.is_file():
        return f"ERROR: {path} is not a file"
    try:
        content = p.read_text()
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

    count = content.count(old_str)
    if count == 0:
        TRACE.log("tool_result", tool="str_replace", ok=False, path=path, error="no_match")
        return (
            f"ERROR: old_str not found in {path}. View the file to confirm exact text "
            f"(whitespace, line endings)."
        )
    if count > 1:
        TRACE.log("tool_result", tool="str_replace", ok=False, path=path,
                  error="multi_match", count=count)
        return (
            f"ERROR: old_str matched {count} times in {path} — must be unique. "
            f"Add more surrounding lines to old_str to disambiguate."
        )

    new_content = content.replace(old_str, new_str, 1)
    p.write_text(new_content)

    syntax_err = _maybe_syntax_check(p)
    msg = f"replaced 1 occurrence in {path} ({len(content)} → {len(new_content)} bytes)"
    if syntax_err:
        msg += f"\n\nWARNING: per-edit syntax check failed:\n{syntax_err}"
        TRACE.log("syntax_check", path=path, ok=False, error=syntax_err[:500])
    elif p.suffix in SYNTAX_CHECK_EXTENSIONS:
        TRACE.log("syntax_check", path=path, ok=True)

    TRACE.log("tool_result", tool="str_replace", ok=True, path=path,
              old_chars=len(old_str), new_chars=len(new_str))
    return msg


@tool
def create_file(path: str, content: str) -> str:
    """Create a NEW file. Errors if path already exists — use str_replace for edits.

    Creates parent directories. Runs a per-edit syntax check on .py / .js files.
    """
    try:
        p = _resolve(path)
    except ValueError as e:
        TRACE.log("tool_result", tool="create_file", ok=False, path=path, error=str(e))
        return f"ERROR: {e}"
    if p.exists():
        TRACE.log("tool_result", tool="create_file", ok=False, path=path, error="exists")
        return f"ERROR: {path} already exists. Use str_replace to edit existing files."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)

    syntax_err = _maybe_syntax_check(p)
    msg = f"created {path} ({len(content)} bytes)"
    if syntax_err:
        msg += f"\n\nWARNING: per-edit syntax check failed:\n{syntax_err}"
        TRACE.log("syntax_check", path=path, ok=False, error=syntax_err[:500])
    elif p.suffix in SYNTAX_CHECK_EXTENSIONS:
        TRACE.log("syntax_check", path=path, ok=True)

    TRACE.log("tool_result", tool="create_file", ok=True, path=path, bytes=len(content))
    return msg


@tool
def list_dir(path: str = ".") -> str:
    """List files/subdirs under /workspace/{path}. Directories suffixed with '/'."""
    try:
        p = _resolve(path)
    except ValueError as e:
        return f"ERROR: {e}"
    if not p.exists():
        return f"ERROR: {path} does not exist"
    if not p.is_dir():
        return f"ERROR: {path} is not a directory"
    entries = [child.name + ("/" if child.is_dir() else "") for child in sorted(p.iterdir())]
    return "\n".join(entries) or "(empty)"


# ────────────────────────── tools: server lifecycle ──────────────────────────


@tool
def serve_in_background(command: str, port: int, cwd: str = ".") -> str:
    """Start a long-running server detached. Waits up to 30s for the port to listen.

    For Next.js dev reachable from the Playwright evaluator, MUST bind to 0.0.0.0:
      command='npx next dev -H 0.0.0.0 -p 3000', port=3000

    The site is then reachable at http://langgraph:3000 from the Playwright MCP container.
    Logs go to /workspace/.servers/<port>.log.
    """
    try:
        work = _resolve(cwd)
    except ValueError as e:
        return f"ERROR: {e}"
    if not work.is_dir():
        return f"ERROR: cwd '{cwd}' is not a directory"

    log_dir = WORKSPACE / ".servers"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"{port}.log"

    log_file = open(log_path, "w")
    proc = subprocess.Popen(
        ["bash", "-lc", command],
        cwd=str(work),
        stdout=log_file, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )

    deadline = time.time() + SERVER_PORT_LISTEN_TIMEOUT_SECONDS
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect(("127.0.0.1", port))
                TRACE.log("tool_result", tool="serve_in_background", ok=True, port=port, pid=proc.pid)
                return f"server listening on port {port} (pid={proc.pid}, log=/workspace/.servers/{port}.log)"
            except (ConnectionRefusedError, socket.timeout, OSError):
                time.sleep(0.5)

    if proc.poll() is not None:
        TRACE.log("tool_result", tool="serve_in_background", ok=False, port=port,
                  exit_code=proc.returncode)
        return (
            f"ERROR: process exited with code {proc.returncode} before port {port} listened. "
            f"See /workspace/.servers/{port}.log"
        )
    TRACE.log("tool_result", tool="serve_in_background", ok=False, port=port, error="port_not_listening")
    return (
        f"WARN: process still running (pid={proc.pid}) but port {port} not listening after "
        f"{SERVER_PORT_LISTEN_TIMEOUT_SECONDS}s. See /workspace/.servers/{port}.log"
    )


@tool
def stop_servers() -> str:
    """Kill all background dev servers in this container."""
    for pat in ("next dev", "next start", "npm run", "node server.js"):
        subprocess.run(["pkill", "-f", pat], capture_output=True)
    TRACE.log("tool_result", tool="stop_servers", ok=True)
    return "killed background dev servers"


# ────────────────────────── tools: plan management ──────────────────────────


# LangChain @tool functions can't easily access LangGraph state; use a module-level holder
# that the planner/builder populate before invoking tools and read back after.
# Holds: doc (the v2 plan document), task (current outer task), replan_count (cap tracking).
_plan_holder: dict = {"doc": None, "task": "", "replan_count": 0}


def _empty_plan_doc() -> dict:
    """Canonical empty v2 plan doc. Used as initial state and as a None-coalescing default."""
    return {
        "requirements": [],
        "architecture": {},
        "tasks": [],
        "pending_proposals": [],
    }


def _set_plan_doc(doc: dict) -> None:
    _plan_holder["doc"] = doc


def _get_plan_doc() -> dict:
    doc = _plan_holder.get("doc")
    return doc if doc is not None else _empty_plan_doc()


def _get_tasks() -> list[dict]:
    return _get_plan_doc().get("tasks", [])


def _set_tasks(tasks: list[dict]) -> None:
    doc = _get_plan_doc()
    doc["tasks"] = tasks
    _plan_holder["doc"] = doc


def _set_plan_context(task: str, doc: dict, replan_count: int) -> None:
    """Sync the holder with the current outer-task context. Called from planner_node and builder_node."""
    _plan_holder["task"] = task
    _plan_holder["doc"] = doc
    _plan_holder["replan_count"] = replan_count


def _persist_plan(task: str, doc: dict, replan_count: int) -> None:
    """Atomic write of the v2 plan to CURRENT_PLAN_PATH. tmp + rename so a crash mid-write
    doesn't corrupt."""
    payload = {
        "version": CURRENT_PLAN_VERSION,
        "task": task,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "trace_file": TRACE.path.name if TRACE.path else None,
        "replan_count": replan_count,
        "requirements": doc.get("requirements", []),
        "architecture": doc.get("architecture", {}),
        "tasks": doc.get("tasks", []),
        "pending_proposals": doc.get("pending_proposals", []),
    }
    CURRENT_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CURRENT_PLAN_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(CURRENT_PLAN_PATH)


def _persist_current_plan() -> None:
    """Persist using the current _plan_holder. Called from plan-mutating tools."""
    _persist_plan(
        _plan_holder.get("task", ""),
        _get_plan_doc(),
        _plan_holder.get("replan_count", 0),
    )


def _load_persisted_plan() -> dict | None:
    """Load the prior plan, if any. Always emits a plan_load_failed trace event for any
    non-OK outcome (missing | corrupt | version_mismatch | stale).

    v1 payloads are upconverted to v2 in-flight (logs `plan_upconvert_v1_to_v2`); the returned
    doc carries `_upconverted_from: 1` and empty REQUIREMENTS / ARCHITECTURE which the planner
    skill knows to fill in on next entry.

    Stale plans are still RETURNED (with _stale=True) so the planner can reason about them as
    advisory context — only missing/corrupt/unknown-version return None.
    """
    if not CURRENT_PLAN_PATH.exists():
        TRACE.log("plan_load_failed", reason="missing")
        return None
    try:
        payload = json.loads(CURRENT_PLAN_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        TRACE.log("plan_load_failed", reason="corrupt", error=str(e))
        return None
    found_version = payload.get("version")
    if found_version == 1:
        TRACE.log("plan_upconvert_v1_to_v2", item_count=len(payload.get("items", [])))
        payload = _upconvert_v1_to_v2(payload)
    elif found_version != CURRENT_PLAN_VERSION:
        TRACE.log("plan_load_failed", reason="version_mismatch", found_version=found_version)
        return None
    try:
        updated = datetime.fromisoformat(payload["updated_at"]) if payload.get("updated_at") else None
    except (KeyError, ValueError) as e:
        TRACE.log("plan_load_failed", reason="corrupt", error=f"updated_at: {e}")
        return None
    if updated is not None:
        age = datetime.now(timezone.utc) - updated
        if age > timedelta(hours=STALE_PLAN_HOURS):
            TRACE.log("plan_load_failed", reason="stale",
                      age_hours=round(age.total_seconds() / 3600, 1))
            payload["_stale"] = True
            # fall through — still return; planner uses it as advisory
        payload["_age_hours"] = round(age.total_seconds() / 3600, 2)
    return payload


def _extract_decision_path(decision_text: str) -> str | None:
    m = re.search(r"path:\s*(fresh|continued|replaced)", decision_text, re.IGNORECASE)
    return m.group(1).lower() if m else None


def _extract_decision_rationale(decision_text: str) -> str:
    m = re.search(r"rationale:\s*(.+?)(?=\n[a-z_]+:|\Z)", decision_text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


@tool
def view_plan() -> str:
    """View the full plan: REQUIREMENTS, ARCHITECTURE, TASKS, and any pending architecture proposals."""
    return _render_plan_doc(_get_plan_doc())


@tool
def update_plan_item(id: int, status: str, notes: str = "") -> str:
    """Update a TASKS item. status: todo | doing | done | blocked. notes optional (e.g., why blocked)."""
    if status not in ("todo", "doing", "done", "blocked"):
        return "ERROR: status must be one of: todo, doing, done, blocked"
    for it in _get_tasks():
        if it["id"] == id:
            it["status"] = status
            if notes:
                it["notes"] = notes
            TRACE.log("plan_update", id=id, status=status, notes=notes[:200])
            _persist_current_plan()
            return f"updated item {id}: {status}"
    return f"ERROR: no plan item with id={id}"


@tool
def add_plan_item(text: str, after_id: int | None = None) -> str:
    """Append a new TASKS item, or insert after the given id. Returns the new id."""
    tasks = _get_tasks()
    new_id = max((it["id"] for it in tasks), default=0) + 1
    new_item = {"id": new_id, "text": text, "status": "todo", "notes": ""}
    if after_id is None:
        tasks.append(new_item)
    else:
        for i, it in enumerate(tasks):
            if it["id"] == after_id:
                tasks.insert(i + 1, new_item)
                break
        else:
            return f"ERROR: no plan item with id={after_id}"
    _set_tasks(tasks)
    TRACE.log("plan_add", id=new_id, text=text[:200])
    _persist_current_plan()
    return f"added item {new_id}"


_VALID_PROPOSAL_SECTIONS = ARCHITECTURE_SUBSECTIONS  # stack | file_tree | data_model | key_decisions


@tool
def view_architecture() -> str:
    """View the ARCHITECTURE section of the plan (read-only).

    Returns the planner's locked architecture decisions: stack, file_tree, data_model,
    key_decisions (or summary, for non-coding tasks). To request a change, call
    propose_architecture_change — you cannot edit ARCHITECTURE directly.
    """
    arch = _get_plan_doc().get("architecture", {})
    return _render_architecture(arch)


@tool
def propose_architecture_change(section: str, change: str, rationale: str) -> str:
    """Record a proposed change to ARCHITECTURE for the planner's next review.

    section: one of stack | file_tree | data_model | key_decisions
    change: concrete description of what should change (and to what)
    rationale: why the current architecture decision is wrong here

    Does NOT replan immediately. Keep working under the current architecture; the planner
    sees pending proposals on the next iteration and either accepts (incorporates the change)
    or rejects (architecture stays). For an immediate replan, use revise_plan instead.
    """
    section = (section or "").strip().lower()
    if section not in _VALID_PROPOSAL_SECTIONS:
        return (
            f"ERROR: section must be one of {list(_VALID_PROPOSAL_SECTIONS)} "
            f"(got '{section}'). For non-architecture concerns, use revise_plan or "
            f"request_user_help."
        )
    change = (change or "").strip()
    rationale = (rationale or "").strip()
    if not change or not rationale:
        return "ERROR: both 'change' and 'rationale' are required and must be non-empty."

    doc = _get_plan_doc()
    proposals = list(doc.get("pending_proposals", []))
    proposals.append({"section": section, "change": change, "rationale": rationale})
    doc["pending_proposals"] = proposals
    _set_plan_doc(doc)
    _persist_current_plan()
    TRACE.log(
        "architecture_proposal",
        section=section, change=change[:500], rationale=rationale[:500],
        proposal_count=len(proposals),
    )
    return (
        f"proposal recorded (#{len(proposals)}, section={section}). The planner will review "
        f"on the next iteration; continue working under the current architecture for now."
    )


# ────────────────────────── tools: exit signals ──────────────────────────


# Exit signals from builder tools land in this holder; the builder graph reads it after
# each tool turn to decide whether to terminate.
_exit_holder: dict = {"signal": None, "payload": {}}


def _reset_exit() -> None:
    _exit_holder["signal"] = None
    _exit_holder["payload"] = {}


@tool
def mark_done(verify_command: str, claim: str) -> str:
    """Mark the task complete. Plan must be resolved; runs verify_command first.

    verify_command: the build/test command that proves the work is correct
        (e.g., 'cd cms-agency && npm run build').
    claim: short summary of what you accomplished.

    Plan resolution rules (enforced before verify):
    - Items in 'doing' state cause an error: resolve them via update_plan_item first.
    - Items in 'todo' state are auto-promoted to 'done' (you're claiming the task is complete).
    - Items in 'blocked' state stay blocked.

    If verify_command's exit code != 0, the failure is returned and the loop continues —
    you CANNOT exit until verification passes (or you call request_user_help / give_up).
    """
    tasks = _get_tasks()
    doing_ids = [it["id"] for it in tasks if it["status"] == "doing"]
    if doing_ids:
        return (
            f"ERROR: cannot mark_done while plan items {doing_ids} are still in 'doing' state. "
            f"Update them to 'done' or 'blocked' first via update_plan_item."
        )

    sh = _get_shell()
    result = sh.run(verify_command, timeout=SHELL_COMMAND_TIMEOUT_SECONDS)
    output = result["output"]
    exit_code = result["exit_code"]
    elapsed_ms = result["elapsed_ms"]

    if exit_code == 0:
        # Promote todo → done and persist ONLY after verify passes — otherwise a failed verify
        # would leave the persisted plan claiming completion the work doesn't actually have.
        for it in tasks:
            if it["status"] == "todo":
                it["status"] = "done"
        _set_tasks(tasks)
        _persist_current_plan()
        _exit_holder["signal"] = "done"
        _exit_holder["payload"] = {"claim": claim, "verify_command": verify_command}
        TRACE.log("builder_exit", reason="done",
                  verify_command=verify_command, claim=claim[:500], elapsed_ms=elapsed_ms)
        TRACE.log("task_completed_with_plan", tasks=tasks, claim=claim[:500])
        return f"VERIFIED: {verify_command} passed (exit 0, {elapsed_ms}ms). Builder exiting as 'done'."

    truncated = _truncate_head_tail(output, SHELL_OUTPUT_HEAD_BYTES, SHELL_OUTPUT_TAIL_BYTES)
    TRACE.log("verify_failed", verify_command=verify_command, exit_code=exit_code, elapsed_ms=elapsed_ms)
    return (
        f"VERIFICATION FAILED: {verify_command} returned exit {exit_code}.\n"
        f"You CANNOT exit until this passes. Fix the failure and call mark_done again, "
        f"or call request_user_help / give_up if you cannot proceed.\n\n"
        f"Output:\n{truncated}\n[exit code: {exit_code}]"
    )


@tool
def request_user_help(reason: str, what_you_tried: str) -> str:
    """Exit the builder loop and ask the user for input. Use when genuinely stuck.

    reason: specific question for the user.
    what_you_tried: short summary of approaches already attempted.
    """
    _exit_holder["signal"] = "help"
    _exit_holder["payload"] = {"reason": reason, "what_you_tried": what_you_tried}
    TRACE.log("builder_exit", reason="help", request=reason[:500])
    return "help requested. builder exiting; user will be prompted."


@tool
def give_up(reason: str) -> str:
    """Exit with explicit failure. Use sparingly — only when the task is infeasible as specified."""
    _exit_holder["signal"] = "give_up"
    _exit_holder["payload"] = {"reason": reason}
    TRACE.log("builder_exit", reason="give_up", explanation=reason[:500])
    return "giving up. builder exiting."


@tool
def revise_plan(rationale: str) -> str:
    """Trigger a replan: exit the builder loop and route back to the planner with rationale.

    Use when you discover the plan is fundamentally wrong (missing requirements, wrong
    framework, etc.). Capped at 2 replans per task to prevent loops.
    """
    _exit_holder["signal"] = "replan"
    _exit_holder["payload"] = {"rationale": rationale}
    TRACE.log("builder_exit", reason="replan", rationale=rationale[:500])
    return f"replan signal sent. rationale: {rationale}"


# ────────────────────────── stuck detector ──────────────────────────


def _check_stuck(state: "BuilderState") -> str | None:
    """Return a stuck-injection message if any signal fires, else None."""
    edits = state.get("edit_history", [])
    shells = state.get("shell_history", [])
    tools_h = state.get("tool_history", [])

    # Edit churn
    if len(edits) >= STUCK_EDIT_REPEAT_THRESHOLD:
        recent = edits[-STUCK_EDIT_WINDOW:]
        counts = Counter((e["file"], e["fingerprint"]) for e in recent)
        for (file, _fp), count in counts.items():
            if count >= STUCK_EDIT_REPEAT_THRESHOLD:
                TRACE.log("stuck_fire", signal="edit_repeat", file=file, count=count)
                return (
                    f"STUCK DETECTED: same edit applied to {file} {count} times in the last "
                    f"{STUCK_EDIT_WINDOW} edits without resolving the underlying problem. "
                    f"STOP repeating. Either:\n"
                    f"- view_file related/imported files for context you may be missing\n"
                    f"- shell('grep -r \"<symbol>\" .') to find usages\n"
                    f"- request_user_help if you can't make progress"
                )

    # Build error stagnation
    builds = [s for s in shells if s.get("is_build")]
    if len(builds) >= STUCK_BUILD_HISTORY:
        recent = builds[-STUCK_BUILD_HISTORY:]
        fps = [b["error_fingerprint"] for b in recent if b.get("error_fingerprint")]
        if fps:
            most_common, count = Counter(fps).most_common(1)[0]
            if count >= STUCK_BUILD_ERROR_REPEAT:
                TRACE.log("stuck_fire", signal="build_error_repeat", count=count)
                return (
                    f"STUCK DETECTED: the same build error has occurred {count} times in the "
                    f"last {STUCK_BUILD_HISTORY} build attempts. STOP applying the same fix. "
                    f"Re-read the error carefully, view related files, consider a different "
                    f"approach. If still blocked, request_user_help."
                )

    # Tool repetition
    if len(tools_h) >= STUCK_TOOL_REPEAT:
        tail = tools_h[-STUCK_TOOL_REPEAT:]
        if all(t == tail[0] for t in tail):
            TRACE.log("stuck_fire", signal="tool_repeat", tool=tail[0][0])
            return (
                f"STUCK DETECTED: you've called {tail[0][0]} with identical arguments "
                f"{STUCK_TOOL_REPEAT} times in a row. The result hasn't changed; doing it again "
                f"won't help. Try a different approach."
            )

    return None


# ────────────────────────── LLMs ──────────────────────────


def _openrouter_llm(model: str) -> ChatOpenAI:
    base = os.environ.get("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    extra: dict = {}
    if "openrouter" in base:
        provider_cfg: dict = {"require_parameters": True}
        pinned = os.environ.get("OPENROUTER_PROVIDERS", "").strip()
        if pinned:
            provider_cfg["order"] = [p.strip() for p in pinned.split(",") if p.strip()]
            provider_cfg["allow_fallbacks"] = False
        extra["extra_body"] = {"provider": provider_cfg}
    return ChatOpenAI(
        model=model,
        base_url=base,
        api_key=os.environ.get("OPENAI_API_KEY", "sk-no-key-required"),
        **extra,
    )


planner_llm = ChatAnthropic(
    model=os.environ.get("PLANNER_MODEL", "claude-sonnet-4-6"),
    max_tokens=8000,
)
builder_llm = _openrouter_llm(os.environ.get("BUILDER_MODEL", "qwen/qwen3-coder-next"))
evaluator_llm = _openrouter_llm(os.environ.get("EVAL_MODEL", "qwen/qwen3.6-27b"))


# ────────────────────────── prompts ──────────────────────────


PLANNER_PROMPT = _load_skill("planning")

BUILDER_BASE_SYSTEM_PROMPT = _load_skill("building")

EVALUATOR_SYSTEM_PROMPT = _load_skill("evaluating")


# ────────────────────────── outer state ──────────────────────────


class State(TypedDict):
    task: str
    iteration: int
    plan: dict  # v2 plan doc: {requirements, architecture, tasks, pending_proposals}
    builder_instructions: str
    evaluator_instructions: str
    builder_summary: str
    builder_exit_signal: str
    builder_exit_payload: dict
    eval_verdict: str
    eval_notes: str
    replan_count: int  # how many times the builder has triggered revise_plan in this task


# ────────────────────────── builder state graph ──────────────────────────


class BuilderState(TypedDict):
    messages: Annotated[list, add_messages]
    plan: dict  # v2 plan doc
    step: int
    max_steps: int
    edit_history: list  # [{file, fingerprint, step}]
    shell_history: list  # [{cmd, exit_code, error_fingerprint, is_build, step}]
    tool_history: list  # [(tool_name, args_hash)]
    stuck_injections: int
    no_tool_call_streak: int  # consecutive turns where the model produced text but no tool call


def _builder_tools() -> list:
    return [
        view_file, str_replace, create_file,
        shell, shell_reset, list_dir,
        serve_in_background, stop_servers,
        view_plan, update_plan_item, add_plan_item,
        view_architecture, propose_architecture_change,
        mark_done, request_user_help, give_up, revise_plan,
    ]


def _render_builder_system(state: BuilderState) -> str:
    step = state["step"]
    max_steps = state["max_steps"]
    remaining = max_steps - step
    parts = [BUILDER_BASE_SYSTEM_PROMPT]
    parts.append("\n" + _render_plan_doc(state["plan"]))
    parts.append(f"\n# STEP BUDGET\nStep {step + 1} of {max_steps}. {remaining} tool calls remaining.")
    if remaining <= 1:
        parts.append("FINAL STEP. Either call mark_done with a passing verify_command, request_user_help, or give_up.")
    elif remaining <= BUILDER_BUDGET_WARNING_THRESHOLD:
        parts.append(f"BUDGET WARNING: {remaining} steps left. Wrap up — call mark_done or request_user_help soon.")
    return "\n".join(parts)


async def builder_model_node(state: BuilderState) -> dict:
    """Render system message + stuck injection, invoke model."""
    sys_msg = SystemMessage(content=_render_builder_system(state))

    extra: list = []
    new_stuck_count = state.get("stuck_injections", 0)
    stuck_msg = _check_stuck(state)
    if stuck_msg:
        new_stuck_count += 1
        extra.append(SystemMessage(content=stuck_msg))
        TRACE.log("stuck_injection", count=new_stuck_count)
        if new_stuck_count >= STUCK_INJECTION_CAP:
            _exit_holder["signal"] = "stuck"
            _exit_holder["payload"] = {"injections": new_stuck_count}
            TRACE.log("builder_exit", reason="stuck", injections=new_stuck_count)
            return {"stuck_injections": new_stuck_count}

    full_messages = [sys_msg] + extra + state["messages"]
    llm_with_tools = builder_llm.bind_tools(_builder_tools())
    response = await llm_with_tools.ainvoke(full_messages)
    return {"messages": [response], "stuck_injections": new_stuck_count}


async def builder_tools_node(state: BuilderState) -> dict:
    """Dispatch tool calls; update edit/shell/tool history for stuck detection."""
    last = state["messages"][-1]
    if not getattr(last, "tool_calls", None):
        # Model produced text without a tool call. Standard ReAct would exit here, but we have
        # explicit exit tools (mark_done / request_user_help / give_up) the model is supposed to
        # call. Inject a reminder and let the loop continue; only exit after enough consecutive
        # text-only turns to know the model isn't going to engage.
        streak = state.get("no_tool_call_streak", 0) + 1
        if streak >= NO_TOOL_CALL_REMINDER_CAP:
            _exit_holder["signal"] = "abandoned"
            _exit_holder["payload"] = {
                "final_text": str(last.content or "")[:1000],
                "no_tool_call_streak": streak,
            }
            TRACE.log("builder_exit", reason="abandoned",
                      final_text=str(last.content or "")[:500], streak=streak)
            return {"no_tool_call_streak": streak}
        TRACE.log("no_tool_call_reminder", streak=streak)
        reminder = SystemMessage(content=(
            "REMINDER: you produced text but no tool call. You MUST call one of the exit tools "
            "to end the loop:\n"
            "- mark_done(verify_command, claim): if the work is complete (runs verify_command; "
            "only succeeds on exit code 0)\n"
            "- request_user_help(reason, what_you_tried): if you're stuck and need human input\n"
            "- give_up(reason): if the task is infeasible as specified\n\n"
            "Otherwise, continue working: call the appropriate tool for your next concrete step."
        ))
        return {"messages": [reminder], "no_tool_call_streak": streak}

    tools_by_name = {t.name: t for t in _builder_tools()}

    # Inject current plan into holder so plan tools can read/mutate it
    _set_plan_doc(state["plan"])

    new_messages: list = []
    new_edits = list(state.get("edit_history", []))
    new_shells = list(state.get("shell_history", []))
    new_tools = list(state.get("tool_history", []))
    step = state["step"]

    for tc in last.tool_calls:
        name = tc["name"]
        args = tc.get("args", {})
        TRACE.set_step(step + 1)
        TRACE.log("tool_call", tool=name, args=args)

        if name not in tools_by_name:
            result = f"ERROR: unknown tool {name}"
        else:
            try:
                t = tools_by_name[name]
                if hasattr(t, "ainvoke"):
                    result = await t.ainvoke(args)
                else:
                    result = t.invoke(args)
            except Exception as e:
                result = f"ERROR: {type(e).__name__}: {e}"
                TRACE.log("tool_exception", tool=name, error=str(e))

        if not isinstance(result, str):
            result = str(result)

        new_messages.append(ToolMessage(content=result, tool_call_id=tc["id"]))

        # Track for stuck detection
        new_tools.append((name, _hash_short(json.dumps(args, sort_keys=True, default=str))))
        if name == "str_replace":
            new_edits.append({
                "file": args.get("path", ""),
                "fingerprint": _hash_short(args.get("old_str", "") + "→" + args.get("new_str", "")),
                "step": step + 1,
            })
        elif name == "create_file":
            new_edits.append({
                "file": args.get("path", ""),
                "fingerprint": _hash_short("create:" + str(args.get("content", ""))[:1000]),
                "step": step + 1,
            })
        elif name == "shell":
            cmd = args.get("command", "")
            is_build = _is_build_command(cmd)
            err_fp = _build_error_fingerprint(result) if is_build else None
            exit_code = -1
            m = re.search(r"\[exit code: (-?\d+)\]", result)
            if m:
                exit_code = int(m.group(1))
            new_shells.append({
                "cmd": cmd[:200],
                "exit_code": exit_code,
                "error_fingerprint": err_fp,
                "is_build": is_build,
                "step": step + 1,
            })

    return {
        "messages": new_messages,
        "plan": _get_plan_doc(),
        "edit_history": new_edits,
        "shell_history": new_shells,
        "tool_history": new_tools,
        "step": step + 1,
        "no_tool_call_streak": 0,  # reset: model engaged with tools this turn
    }


def after_model_router(state: BuilderState) -> Literal["tools", "__end__"]:
    if _exit_holder["signal"] is not None:
        return END
    return "tools"


def after_tools_router(state: BuilderState) -> Literal["model", "__end__"]:
    if _exit_holder["signal"] is not None:
        return END
    if state["step"] >= state["max_steps"]:
        _exit_holder["signal"] = "budget_exhausted"
        TRACE.log("builder_exit", reason="budget_exhausted", step=state["step"])
        return END
    return "model"


def build_builder_graph():
    g = StateGraph(BuilderState)
    g.add_node("model", builder_model_node)
    g.add_node("tools", builder_tools_node)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model_router, {"tools": "tools", END: END})
    g.add_conditional_edges("tools", after_tools_router, {"model": "model", END: END})
    return g.compile()


_builder_graph = build_builder_graph()


def _format_builder_summary(state: BuilderState, exit_signal: str, exit_payload: dict) -> str:
    parts = [f"Builder exited: {exit_signal} (after {state['step']} steps)"]
    if exit_signal == "done":
        parts.append(f"Verification: `{exit_payload.get('verify_command', '?')}` passed.")
        parts.append(f"Claim: {exit_payload.get('claim', '?')}")
    elif exit_signal == "help":
        parts.append(f"Help requested: {exit_payload.get('reason', '?')}")
        parts.append(f"What was tried: {exit_payload.get('what_you_tried', '?')}")
    elif exit_signal == "give_up":
        parts.append(f"Reason: {exit_payload.get('reason', '?')}")
    elif exit_signal == "stuck":
        parts.append(f"Stuck-detector cap reached ({exit_payload.get('injections', '?')} injections).")
    elif exit_signal == "abandoned":
        parts.append("Model produced text but no tool call. Final text:")
        parts.append(_truncate_simple(exit_payload.get("final_text", ""), 500))
    elif exit_signal == "budget_exhausted":
        parts.append(f"Budget of {state['max_steps']} steps exhausted before mark_done.")
    tasks = state["plan"].get("tasks", []) if isinstance(state["plan"], dict) else []
    done = sum(1 for t in tasks if t["status"] == "done")
    parts.append(f"Plan progress: {done}/{len(tasks)} tasks done.")
    return "\n".join(parts)


async def builder_node(outer_state: State) -> dict:
    print(f"\n━━━ BUILDER (iteration {outer_state['iteration']}) ━━━")
    TRACE.log("builder_start", iteration=outer_state["iteration"])
    _reset_exit()

    # Sync holder so plan-mutating tools and mark_done can persist with the right task/replan_count.
    plan_doc = outer_state.get("plan") or _empty_plan_doc()
    _set_plan_context(
        outer_state["task"],
        plan_doc,
        outer_state.get("replan_count", 0),
    )

    initial_messages: list = [HumanMessage(content=outer_state["builder_instructions"])]
    builder_state: BuilderState = {
        "messages": initial_messages,
        "plan": plan_doc,
        "step": 0,
        "max_steps": MAX_BUILDER_STEPS,
        "edit_history": [],
        "shell_history": [],
        "tool_history": [],
        "stuck_injections": 0,
        "no_tool_call_streak": 0,
    }

    final = await _builder_graph.ainvoke(
        builder_state,
        config={"recursion_limit": MAX_BUILDER_STEPS * 4 + 20},
    )

    exit_signal = _exit_holder["signal"] or "budget_exhausted"
    exit_payload = dict(_exit_holder["payload"])
    summary = _format_builder_summary(final, exit_signal, exit_payload)

    print(f"\n  Builder exit: {exit_signal} (step {final['step']}/{MAX_BUILDER_STEPS})")
    return {
        "builder_summary": summary,
        "builder_exit_signal": exit_signal,
        "builder_exit_payload": exit_payload,
        "plan": final["plan"],
    }


# ────────────────────────── planner ──────────────────────────


def _apply_planner_merge(
    prior_doc: dict | None,
    path: str,
    new_requirements: list[str],
    new_architecture: dict[str, str],
    new_tasks: list[dict],
    rationale: str,
) -> tuple[list[str], dict[str, str], list[dict]]:
    """Apply the # DECISION path's merge rules. Returns final (requirements, architecture, tasks).

    - continued: REQUIREMENTS append (log requirement_duplicate per exact-text repeat, no dedupe);
      ARCHITECTURE replaces per emitted sub-section, keeps unemitted sub-sections;
      TASKS append (log task_duplicate per exact-text repeat, no dedupe), renumbered after prior.
    - replaced: prior is discarded; new sections become the plan. Logs prior_plan_abandoned for
      any non-done prior tasks.
    - fresh (or anomalous fallback): new sections become the plan; no merge.
    """
    if path == "continued" and prior_doc:
        prior_reqs = list(prior_doc.get("requirements", []))
        merged_requirements = list(prior_reqs)
        prior_req_set = set(prior_reqs)
        for r in new_requirements:
            if r in prior_req_set:
                TRACE.log("requirement_duplicate", text=r[:200])
            merged_requirements.append(r)

        merged_architecture = dict(prior_doc.get("architecture", {}))
        for subname, content in new_architecture.items():
            merged_architecture[subname] = content  # replace per sub-section

        prior_tasks = prior_doc.get("tasks", [])
        prior_task_texts = {t["text"] for t in prior_tasks}
        for nt in new_tasks:
            if nt["text"] in prior_task_texts:
                TRACE.log("task_duplicate", text=nt["text"][:200])
        base_id = max((t["id"] for t in prior_tasks), default=0)
        renumbered = [{**t, "id": base_id + i + 1} for i, t in enumerate(new_tasks)]
        merged_tasks = prior_tasks + renumbered
        return merged_requirements, merged_architecture, merged_tasks

    if path == "replaced" and prior_doc:
        abandoned = [t for t in prior_doc.get("tasks", []) if t["status"] != "done"]
        if abandoned:
            TRACE.log("prior_plan_abandoned", abandoned_items=abandoned, count=len(abandoned))
        TRACE.log("planner_replaced", rationale=rationale[:500])

    return list(new_requirements), dict(new_architecture), list(new_tasks)


def _apply_proposal_review(
    prior_doc: dict | None,
    path: str,
    proposal_review_text: str,
) -> None:
    """Emit proposal_review trace events for every pending proposal.

    Skill-level rules:
    - replaced path → all pending proposals implicitly rejected (architecture being replaced).
    - continued path → must have # PROPOSAL_REVIEW; if section missing, log
      proposal_review_section_missing and reject all; per-entry skips → proposal_review_missing
      and reject that one.
    - fresh path → no prior, no proposals (nothing to do).

    pending_proposals is cleared from the final doc by the caller regardless — proposals do not
    carry over across planner runs.
    """
    pending = (prior_doc or {}).get("pending_proposals", [])
    if not pending:
        return

    if path == "replaced":
        for i, p in enumerate(pending, start=1):
            TRACE.log(
                "proposal_review", index=i, accepted=False,
                section=p.get("section"),
                rationale="auto-reject: prior architecture replaced (path=replaced)",
            )
        return

    if path != "continued":
        # fresh with no prior → pending is empty (handled above). Anomalous: log + reject all.
        TRACE.log("proposal_review_section_missing",
                  proposal_count=len(pending), note=f"unexpected path={path} with pending proposals")
        for i, p in enumerate(pending, start=1):
            TRACE.log("proposal_review", index=i, accepted=False,
                      section=p.get("section"),
                      rationale=f"auto-reject: unexpected path={path} with pending proposals")
        return

    # path == "continued": PROPOSAL_REVIEW is required
    if not (proposal_review_text or "").strip():
        TRACE.log("proposal_review_section_missing", proposal_count=len(pending))
        for i, p in enumerate(pending, start=1):
            TRACE.log("proposal_review", index=i, accepted=False,
                      section=p.get("section"),
                      rationale="auto-reject: PROPOSAL_REVIEW section missing from planner output")
        return

    entries = _parse_proposal_review(proposal_review_text)
    by_index = {e["index"]: e for e in entries}
    for i, p in enumerate(pending, start=1):
        e = by_index.get(i)
        if e is None:
            TRACE.log("proposal_review_missing", index=i, section=p.get("section"))
            TRACE.log("proposal_review", index=i, accepted=False,
                      section=p.get("section"),
                      rationale="auto-reject: missing from PROPOSAL_REVIEW section")
        else:
            TRACE.log(
                "proposal_review", index=i,
                accepted=(e["decision"] == "accepted"),
                section=p.get("section"),
                rationale=e["rationale"][:500],
            )


async def planner_node(state: State) -> dict:
    iteration = state.get("iteration", 0) + 1
    task = state["task"]
    TRACE.set_iter(iteration)
    TRACE.set_step(0)

    # Increment replan_count when entering after a builder revise_plan signal
    replan_count = state.get("replan_count", 0)
    came_from_revise = state.get("builder_exit_signal") == "replan"
    if came_from_revise:
        replan_count += 1

    # Determine prior_doc to merge against:
    # - iteration 1: load from disk (cross-task continuation)
    # - iteration > 1: take from state (in-PBE replan; same task, evolving plan)
    if iteration == 1:
        prior = _load_persisted_plan()
        prior_doc = prior  # may be None
    else:
        prior = None
        prior_doc = state.get("plan")
        if not prior_doc or not isinstance(prior_doc, dict):
            prior_doc = None

    pending_proposals = (prior_doc or {}).get("pending_proposals", [])

    # Section list to request: include PROPOSAL_REVIEW when prior had pending proposals.
    sections_to_emit = ["# DECISION"]
    if pending_proposals:
        sections_to_emit.append("# PROPOSAL_REVIEW (required if path=continued)")
    sections_to_emit += [
        "# REQUIREMENTS", "# ARCHITECTURE", "# TASKS",
        "# BUILDER_INSTRUCTIONS", "# EVALUATOR_INSTRUCTIONS",
    ]
    sections_clause = ", ".join(sections_to_emit)

    # Build the user-message content
    if iteration == 1:
        if prior is not None:
            stale_block = ""
            if prior.get("_stale"):
                stale_block = (
                    f"\n[NOTE: prior plan is older than {STALE_PLAN_HOURS}h "
                    f"(_age_hours={prior['_age_hours']}). Treat as ADVISORY ONLY: default to "
                    f"'replaced' unless the new task explicitly references the prior work.]\n"
                )
            upconvert_block = ""
            if prior.get("_upconverted_from") == 1:
                upconvert_block = (
                    "\n[NOTE: prior plan was upconverted from v1; REQUIREMENTS and ARCHITECTURE "
                    "are empty. Even on 'continued', fill them in by deriving from prior tasks "
                    "and the new user task.]\n"
                )
            prior_doc_view = {
                "requirements": prior.get("requirements", []),
                "architecture": prior.get("architecture", {}),
                "tasks": prior.get("tasks", []),
                "pending_proposals": prior.get("pending_proposals", []),
            }
            prior_block = (
                f"\n\n# PRIOR PLAN CONTEXT\n"
                f"Prior task: {prior['task']}\n"
                f"Prior updated_at: {prior.get('updated_at')}\n"
                f"_age_hours: {prior.get('_age_hours')}\n"
                f"_stale: {prior.get('_stale', False)}\n"
                f"_upconverted_from: {prior.get('_upconverted_from', 0)}\n"
                f"Prior plan:\n```json\n{json.dumps(prior_doc_view, indent=2)}\n```"
                f"{stale_block}{upconvert_block}"
            )
        else:
            prior_block = "\n\n# PRIOR PLAN CONTEXT\n(no prior plan exists)"
        msg = (
            f"USER TASK:\n{task}{prior_block}\n\n"
            f"Decide path (fresh | continued | replaced), then emit {sections_clause}."
        )
    else:
        plan_render = _render_plan_doc(prior_doc) if prior_doc else "(none)"
        revise_block = ""
        if came_from_revise:
            revise_block = (
                f"\nBUILDER CALLED revise_plan WITH RATIONALE:\n"
                f"{state.get('builder_exit_payload', {}).get('rationale', '?')}\n"
                f"(Replan count after this iteration: {replan_count}/{MAX_REPLANS})\n"
            )
        msg = (
            f"USER TASK:\n{task}\n\n"
            f"PRIOR ITERATION ({iteration - 1}) PLAN:\n{plan_render}\n\n"
            f"PRIOR BUILDER SUMMARY:\n{state.get('builder_summary', '(none)')}\n\n"
            f"PRIOR BUILDER EXIT SIGNAL: {state.get('builder_exit_signal', '?')}\n"
            f"{revise_block}"
            f"PRIOR EVALUATOR VERDICT: {state.get('eval_verdict', 'continue')}\n"
            f"PRIOR EVALUATOR NOTES:\n{state.get('eval_notes', '(none)')}\n\n"
            f"Write the revised plan and instructions for iteration {iteration}. "
            f"Emit {sections_clause} (path: continued | replaced)."
        )

    print(f"\n━━━ PLANNER (iteration {iteration}) ━━━")
    response = await planner_llm.ainvoke([
        SystemMessage(content=PLANNER_PROMPT),
        HumanMessage(content=msg),
    ])
    text = response.content if isinstance(response.content, str) else str(response.content)

    # Parse all sections
    decision_text = _extract_section(text, "DECISION")
    parsed_path = _extract_decision_path(decision_text)
    rationale = _extract_decision_rationale(decision_text) or "(none provided)"

    proposal_review_text = _extract_section(text, "PROPOSAL_REVIEW")
    requirements_text = _extract_section(text, "REQUIREMENTS")
    architecture_text = _extract_section(text, "ARCHITECTURE")
    tasks_text = _extract_section(text, "TASKS")
    bi = _extract_section(text, "BUILDER_INSTRUCTIONS")
    ei = _extract_section(text, "EVALUATOR_INSTRUCTIONS")

    new_requirements = _parse_requirements(requirements_text)
    new_architecture = _parse_architecture(architecture_text)
    new_tasks = _parse_tasks(tasks_text)

    # Determine effective path. Anomaly cases: 'fresh' with prior → treat as replaced;
    # non-fresh with no prior → treat as fresh.
    if parsed_path is None:
        path = "fresh" if prior_doc is None else "continued"
        TRACE.log("planner_decision_anomaly",
                  note=f"missing or unparseable path; defaulting to {path}")
    elif parsed_path == "fresh" and prior_doc is not None:
        TRACE.log("planner_decision_anomaly",
                  note="path='fresh' but prior plan existed; treating as replaced")
        path = "replaced"
    elif parsed_path != "fresh" and prior_doc is None:
        TRACE.log("planner_decision_anomaly",
                  note=f"path='{parsed_path}' but no prior plan existed; treating as fresh")
        path = "fresh"
    else:
        path = parsed_path

    # Apply merge + emit proposal_review trace
    final_requirements, final_architecture, final_tasks = _apply_planner_merge(
        prior_doc, path, new_requirements, new_architecture, new_tasks, rationale,
    )
    _apply_proposal_review(prior_doc, path, proposal_review_text)

    # pending_proposals is always cleared after planner review.
    final_doc: dict = {
        "requirements": final_requirements,
        "architecture": final_architecture,
        "tasks": final_tasks,
        "pending_proposals": [],
    }

    TRACE.log(
        "planner_decision",
        path=path,
        rationale=rationale[:500],
        prior_existed=prior_doc is not None,
        prior_stale=bool(prior and prior.get("_stale")),
        prior_age_hours=prior["_age_hours"] if prior else None,
        prior_tasks=len((prior_doc or {}).get("tasks", [])),
        prior_requirements=len((prior_doc or {}).get("requirements", [])),
        prior_pending_proposals=len(pending_proposals),
        new_tasks=len(new_tasks),
        new_requirements=len(new_requirements),
        new_architecture_subsections=sorted(new_architecture.keys()),
        final_tasks=len(final_tasks),
        final_requirements=len(final_requirements),
        final_architecture_subsections=sorted(final_architecture.keys()),
    )
    print(f"Plan ({len(final_tasks)} tasks, {len(final_requirements)} reqs, "
          f"path={path}):\n{_truncate_simple(_render_plan_doc(final_doc), 800)}\n")
    TRACE.log("planner_done",
              items=len(final_tasks), tasks_text=tasks_text[:1000], path=path)

    # Persist + sync the holder so plan-mutating tools and mark_done can find task/replan_count
    _set_plan_context(task, final_doc, replan_count)
    _persist_plan(task, final_doc, replan_count)

    return {
        "iteration": iteration,
        "plan": final_doc,
        "builder_instructions": bi,
        "evaluator_instructions": ei,
        "replan_count": replan_count,
    }


# ────────────────────────── evaluator ──────────────────────────


_evaluator_holder: dict = {"agent": None}


async def build_evaluator_subagent():
    mcp_url = os.environ.get("PLAYWRIGHT_MCP_URL", "http://playwright-mcp:8931/sse")
    client = MultiServerMCPClient({
        "playwright": {"url": mcp_url, "transport": "sse"},
    })
    try:
        mcp_tools = await client.get_tools()
        print(f"  Loaded {len(mcp_tools)} Playwright MCP tools")
    except Exception as e:
        print(f"  WARN: failed to connect to Playwright MCP at {mcp_url}: {type(e).__name__}: {e}")
        # asyncio.TaskGroup wraps real causes; surface them so we know what to fix.
        for sub in getattr(e, "exceptions", ()):
            print(f"    cause: {type(sub).__name__}: {sub}")
        print("  WARN: evaluator will run with code-only tools (no screenshots).")
        mcp_tools = []
    return create_agent(
        evaluator_llm,
        tools=[view_file, list_dir, run_shell_oneshot, serve_in_background] + mcp_tools,
        **{_AGENT_PROMPT_KWARG: EVALUATOR_SYSTEM_PROMPT},
    )


async def _stream_subagent(subagent, prompt: str, label: str, recursion_limit: int) -> str:
    final_text = ""
    async for event in subagent.astream(
        {"messages": [HumanMessage(content=prompt)]},
        config={"recursion_limit": recursion_limit},
        stream_mode="updates",
    ):
        for _, node_output in event.items():
            for msg in node_output.get("messages", []):
                if getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        print(f"  [{label}] {tc['name']}({_format_args(tc.get('args', {}))})")
                        TRACE.log("eval_tool_call", tool=tc["name"], args=tc.get("args", {}))
                elif getattr(msg, "type", None) == "tool":
                    body = (msg.content or "").strip().replace("\n", "\\n")
                    print(f"  [{label}-result] {msg.name} -> {_truncate_simple(body)}")
                    TRACE.log("eval_tool_result", tool=msg.name, output_chars=len(msg.content or ""))
                elif msg.content:
                    final_text = msg.content
    return final_text


async def evaluator_node(state: State) -> dict:
    if _evaluator_holder["agent"] is None:
        _evaluator_holder["agent"] = await build_evaluator_subagent()
    print(f"\n━━━ EVALUATOR (iteration {state['iteration']}) ━━━")
    TRACE.log("evaluator_start", iteration=state["iteration"])

    plan_doc = state.get("plan") or _empty_plan_doc()
    plan_render = _render_plan_doc(plan_doc)
    requirements_render = _render_requirements(plan_doc.get("requirements", []))
    prompt = (
        f"REQUIREMENTS (load-bearing contract — verify EACH one is satisfied):\n"
        f"{requirements_render}\n\n"
        f"PLAN (current state, includes ARCHITECTURE and TASKS):\n{plan_render}\n\n"
        f"YOUR VERIFICATION INSTRUCTIONS (planner's specific asks for this iteration):\n"
        f"{state['evaluator_instructions']}\n\n"
        f"BUILDER SUMMARY (claim is a starting point, NOT evidence — verify by observation):\n"
        f"{state['builder_summary']}\n\n"
        f"Verify the work and emit your verdict block at the end."
    )
    text = await _stream_subagent(_evaluator_holder["agent"], prompt, "eval", EVAL_RECURSION_LIMIT)
    verdict = _extract_verdict(text)
    notes = _extract_notes(text) or text
    print(f"\n  VERDICT: {verdict}")
    print(f"  NOTES: {_truncate_simple(notes, 400)}")
    TRACE.log("verdict", verdict=verdict, notes=notes[:1000])
    return {"eval_verdict": verdict, "eval_notes": notes}


# ────────────────────────── outer routers + graph ──────────────────────────


def route_after_builder(state: State) -> Literal["evaluator", "planner", "__end__"]:
    sig = state.get("builder_exit_signal")
    if sig in ("help", "give_up"):
        print(f"\n━━━ Builder exited '{sig}': ending task. ━━━")
        return END
    if sig == "replan":
        if state.get("replan_count", 0) >= MAX_REPLANS:
            print(f"\n━━━ Stopped: max replans ({MAX_REPLANS}) reached. ━━━")
            TRACE.log("replan_capped", replan_count=state.get("replan_count", 0))
            return END
        return "planner"
    if state["iteration"] >= MAX_PBE_ITERATIONS:
        print(f"\n━━━ Stopped: max PBE iterations ({MAX_PBE_ITERATIONS}) reached. ━━━")
        return END
    # done, budget_exhausted, stuck, abandoned → let evaluator judge
    return "evaluator"


def route_after_eval(state: State) -> Literal["planner", "builder", "__end__"]:
    if state["iteration"] >= MAX_PBE_ITERATIONS:
        print(f"\n━━━ Stopped: max PBE iterations ({MAX_PBE_ITERATIONS}) reached. ━━━")
        return END
    verdict = state["eval_verdict"]
    if verdict == "done":
        print(f"\n━━━ Done in {state['iteration']} iteration(s). ━━━")
        return END
    if verdict == "replan":
        return "planner"
    return "builder"


def build_outer_graph():
    g = StateGraph(State)
    g.add_node("planner", planner_node)
    g.add_node("builder", builder_node)
    g.add_node("evaluator", evaluator_node)
    g.add_edge(START, "planner")
    g.add_edge("planner", "builder")
    g.add_conditional_edges("builder", route_after_builder, {
        "evaluator": "evaluator", "planner": "planner", END: END,
    })
    g.add_conditional_edges("evaluator", route_after_eval, {
        "planner": "planner", "builder": "builder", END: END,
    })
    return g.compile()


graph = build_outer_graph()


# ────────────────────────── main loop ──────────────────────────


async def main():
    print(f"Planner:   {planner_llm.model}  (Anthropic)")
    print(f"Builder:   {builder_llm.model_name}  (via {builder_llm.openai_api_base})")
    print(f"Evaluator: {evaluator_llm.model_name}  (via {evaluator_llm.openai_api_base})")
    print(f"Each task: planner → builder (max {MAX_BUILDER_STEPS} steps) → evaluator, looped (max {MAX_PBE_ITERATIONS} iterations).")
    print(f"Trace dir: {TRACE_DIR}")
    print("Ctrl-D to exit.\n")

    while True:
        try:
            user_input = input("Task: ")
        except EOFError:
            print()
            break
        if not user_input.strip():
            continue

        trace_path = TRACE.start_task(user_input)
        print(f"Trace: {trace_path}\n")

        try:
            final = await graph.ainvoke(
                {"task": user_input, "iteration": 0, "plan": _empty_plan_doc(), "replan_count": 0},
                config={"recursion_limit": 200},
            )
            TRACE.end_task(reason="completed", final_iter=final.get("iteration"),
                           final_verdict=final.get("eval_verdict"),
                           builder_exit=final.get("builder_exit_signal"))
        except Exception as e:
            TRACE.end_task(reason="exception", error=str(e))
            raise
        print()


if __name__ == "__main__":
    asyncio.run(main())
