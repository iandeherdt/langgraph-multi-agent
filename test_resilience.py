"""Manual resilience tests. Run inside the langgraph container:

    docker compose run --rm langgraph python /app/test_resilience.py

Covers:
  1. Retry classification — _is_retryable_error decides correctly per status code.
  2. Streaming retry — _ainvoke_streaming retries transient errors and returns successfully.
  3. Streaming retry — exhausted attempts re-raise.
  4. Streaming retry — non-retryable status raises immediately (no retry).
  5. Schema-mismatch detection — _check_resume_compatibility returns False on version skew.
  6. Verify-completion: token required for mark_done.
  7. Verify-completion: token is single-use.
  8. Verify-completion: verdict cap enforced.
  9. Verify-completion: error cap is separate from verdict cap.
 10. Verify-completion: unparseable advisor response counts as error, no token issued.
 11. Verify-completion: recent-output match (with and without).

For checkpoint resume, see the manual recipe in README.md (kill mid-builder, restart, confirm).
"""
import asyncio
import sys
import types

import openai

sys.path.insert(0, "/app")
import graph

# Speed up retries for the test
graph.MODEL_RETRY_BASE_DELAY = 0  # no actual sleeping


def _make_status_error(status: int, message: str = "boom"):
    """Build an openai.APIStatusError with a given status_code attribute."""
    err = openai.APIError(message=message, request=None, body=None)
    err.status_code = status
    return err


def test_retry_classification():
    print("[1/5] _is_retryable_error classifies status codes correctly")
    # Retryable
    for s in (500, 502, 503, 504, 529):
        e = _make_status_error(s)
        assert graph._is_retryable_error(e), f"status {s} should be retryable"
    # Not retryable (4xx)
    for s in (400, 401, 403, 404, 429):  # 429 deliberately excluded
        e = _make_status_error(s)
        assert not graph._is_retryable_error(e), f"status {s} should NOT be retryable"
    # No status — transport-level (transient)
    transport_err = openai.APIError(message="connection reset", request=None, body=None)
    assert graph._is_retryable_error(transport_err), "no-status error should be retryable"
    print("  PASS")


class _FakeChunk:
    """Mimic AIMessageChunk enough for _ainvoke_streaming."""
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
    def __add__(self, other):
        return _FakeChunk(
            content=(self.content or "") + (other.content or ""),
            tool_calls=(self.tool_calls or []) + (other.tool_calls or []),
        )


class _FakeLLM:
    """Fake chat model whose astream raises N times then succeeds."""
    def __init__(self, fail_n: int, fail_status: int = 503, success_chunks=None):
        self.fail_n = fail_n
        self.fail_status = fail_status
        self.calls = 0
        self.success_chunks = success_chunks or [_FakeChunk("hello "), _FakeChunk("world")]

    def astream(self, messages):
        self.calls += 1
        attempt = self.calls
        chunks = self.success_chunks
        fail_n = self.fail_n
        fail_status = self.fail_status

        async def gen():
            if attempt <= fail_n:
                # Yield one chunk so we exercise the partial-discard path, then fail.
                yield _FakeChunk("partial-")
                raise _make_status_error(fail_status, f"attempt {attempt} failure")
            for c in chunks:
                yield c
        return gen()


async def test_retry_succeeds_after_two_failures():
    print("[2/5] _ainvoke_streaming retries transient errors and succeeds")
    llm = _FakeLLM(fail_n=2, fail_status=503)
    final = await graph._ainvoke_streaming(llm, [], "test")
    assert llm.calls == 3, f"expected 3 attempts, got {llm.calls}"
    assert final.content == "hello world", f"unexpected accumulated content: {final.content!r}"
    print(f"  PASS — {llm.calls} attempts, final content: {final.content!r}")


async def test_retry_exhausted_raises():
    print("[3/5] _ainvoke_streaming raises after MODEL_RETRY_MAX_ATTEMPTS exhausted")
    llm = _FakeLLM(fail_n=99, fail_status=503)  # always fails
    try:
        await graph._ainvoke_streaming(llm, [], "test")
    except openai.APIError as e:
        assert llm.calls == graph.MODEL_RETRY_MAX_ATTEMPTS, \
            f"expected {graph.MODEL_RETRY_MAX_ATTEMPTS} attempts, got {llm.calls}"
        print(f"  PASS — exhausted after {llm.calls} attempts; final error: {e}")
        return
    raise AssertionError("expected APIError after exhausted retries")


async def test_non_retryable_raises_immediately():
    print("[4/5] _ainvoke_streaming raises immediately on non-retryable status")
    llm = _FakeLLM(fail_n=99, fail_status=401)  # auth error — not retryable
    try:
        await graph._ainvoke_streaming(llm, [], "test")
    except openai.APIError:
        assert llm.calls == 1, f"expected 1 attempt for non-retryable error, got {llm.calls}"
        print(f"  PASS — 401 raised on first attempt, no retries")
        return
    raise AssertionError("expected APIError")


async def test_schema_mismatch_rejects_resume():
    print("[5/5] _check_resume_compatibility rejects mismatched schema_version")
    # Build a saver in-memory and write a checkpoint with a wrong schema_version, then verify
    # the resume check returns False AND emits the trace event.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
        config = {"configurable": {"thread_id": "schema-test", "checkpoint_ns": ""}}
        from langgraph.checkpoint.base import empty_checkpoint
        ckpt = empty_checkpoint()
        # Stamp a deliberately-wrong schema_version
        bad_metadata = {"source": "input", "step": 0, "writes": {}, "schema_version": 999}
        await saver.aput(config, ckpt, bad_metadata, {})

        # Should reject (current version is 1, saved version is 999)
        ok = await graph._check_resume_compatibility(saver, "schema-test")
        assert ok is False, "expected resume check to reject mismatched version"

        # And matching version should accept
        config2 = {"configurable": {"thread_id": "schema-test-ok", "checkpoint_ns": ""}}
        good_metadata = {"source": "input", "step": 0, "writes": {},
                         "schema_version": graph.CHECKPOINT_SCHEMA_VERSION}
        await saver.aput(config2, empty_checkpoint(), good_metadata, {})
        ok2 = await graph._check_resume_compatibility(saver, "schema-test-ok")
        assert ok2 is True, "expected resume check to accept matching version"
    print("  PASS — mismatch rejected, match accepted")


class _FakeAdvisor:
    """Fake advisor LLM whose astream yields a single chunk with the given content."""
    def __init__(self, content: str = "", raise_exc: Exception | None = None):
        self.content = content
        self.raise_exc = raise_exc
        self.model = "fake-advisor"

    def astream(self, messages):
        content = self.content
        raise_exc = self.raise_exc
        async def gen():
            if raise_exc is not None:
                raise raise_exc
            yield _FakeChunk(content=content)
        return gen()


def _reset_verify_world():
    """Clean state between verify_completion tests."""
    graph._reset_verification()
    graph._shell_output_history.clear()
    graph._set_plan_context("test task", graph._empty_plan_doc(), 0)


async def test_mark_done_requires_token():
    print("[6/11] mark_done refuses without verification_token")
    _reset_verify_world()
    r = graph.mark_done.invoke({"verify_command": "true", "claim": "done", "verification_token": ""})
    assert "requires verification_token" in r, f"unexpected: {r}"
    r2 = graph.mark_done.invoke({"verify_command": "true", "claim": "done", "verification_token": "bogus-uuid"})
    assert "does not match" in r2, f"unexpected: {r2}"
    print("  PASS — empty and mismatched tokens both rejected")


async def test_token_is_single_use():
    print("[7/11] verification token is single-use")
    _reset_verify_world()
    # Manually issue a token (skip the real advisor call)
    fake_token = "11111111-2222-3333-4444-555555555555"
    graph._verification_holder["issued_token"] = fake_token
    # First mark_done burns the token (verify_command will probably fail in this test env, but
    # we only care that the token gate accepted then consumed it).
    r1 = graph.mark_done.invoke({"verify_command": "false", "claim": "done", "verification_token": fake_token})
    assert "VERIFICATION FAILED" in r1 or "exit" in r1, f"first call should reach verify, got: {r1}"
    # Second mark_done with same token must reject as reused
    r2 = graph.mark_done.invoke({"verify_command": "false", "claim": "done", "verification_token": fake_token})
    assert "already been used" in r2, f"unexpected on reuse: {r2}"
    print("  PASS — token consumed on first use, rejected on second")


async def test_verdict_cap_enforced():
    print("[8/11] verify_completion verdict cap (3) refuses 4th call")
    _reset_verify_world()
    graph._verification_holder["verdict_count"] = 3  # simulate three prior verdicts
    r = await graph.verify_completion.ainvoke({
        "task_summary": "build a thing", "evidence": ["did stuff"], "verify_command": "true",
    })
    assert "verdict cap reached" in r, f"unexpected: {r}"
    assert "give_up" in r, f"should direct to give_up: {r}"
    print("  PASS — verdict cap refused without calling advisor")


async def test_error_cap_separate_from_verdict_cap():
    print("[9/11] verify_completion error cap (2) is separate from verdict cap")
    _reset_verify_world()
    # Save the real advisor; install a fake that always raises a transient error.
    real = graph.advisor_llm
    graph.advisor_llm = _FakeAdvisor(raise_exc=openai.APIError(message="upstream dead", request=None, body=None))
    try:
        # First two error calls should be allowed and increment error_count
        r1 = await graph.verify_completion.ainvoke({
            "task_summary": "x", "evidence": ["y"], "verify_command": "true",
        })
        assert "advisor unreachable" in r1, f"first: {r1}"
        assert graph._verification_holder["error_count"] == 1
        assert graph._verification_holder["verdict_count"] == 0, "verdict cap must NOT be burned"
        r2 = await graph.verify_completion.ainvoke({
            "task_summary": "x", "evidence": ["y"], "verify_command": "true",
        })
        assert graph._verification_holder["error_count"] == 2
        assert graph._verification_holder["verdict_count"] == 0
        # Third call refused on entry — error cap reached
        r3 = await graph.verify_completion.ainvoke({
            "task_summary": "x", "evidence": ["y"], "verify_command": "true",
        })
        assert "error cap reached" in r3, f"third: {r3}"
        assert "request_user_help" in r3
    finally:
        graph.advisor_llm = real
    print("  PASS — error cap fired without burning verdict cap (verdict_count stayed at 0)")


async def test_unparseable_response_counts_as_error_no_token():
    print("[10/11] unparseable advisor response is an error, no token issued")
    _reset_verify_world()
    real = graph.advisor_llm
    graph.advisor_llm = _FakeAdvisor(content="this is definitely not json {{{ broken")
    try:
        r = await graph.verify_completion.ainvoke({
            "task_summary": "x", "evidence": ["y"], "verify_command": "true",
        })
        assert "unparseable" in r, f"expected unparseable error: {r}"
        assert graph._verification_holder["error_count"] == 1
        assert graph._verification_holder["verdict_count"] == 0
        assert graph._verification_holder["issued_token"] is None
    finally:
        graph.advisor_llm = real
    print("  PASS — unparseable counted as error, no token issued, verdict cap intact")


async def test_recent_output_match():
    print("[11/11] _find_recent_verify_output matches loosely (cd-prefix variation)")
    _reset_verify_world()
    # Push some shell history
    graph._shell_output_history.extend([
        {"command": "ls /tmp", "exit_code": 0, "output": "files...", "timed_out": False, "step": 1},
        {"command": "cd cms-agency && npm run build", "exit_code": 0,
         "output": "✓ Compiled successfully", "timed_out": False, "step": 5},
        {"command": "ls /tmp/foo", "exit_code": 1, "output": "", "timed_out": False, "step": 7},
    ])
    # Builder asks about "npm run build" — should match the second entry (substring both ways)
    match = graph._find_recent_verify_output("npm run build")
    assert match is not None, "should have matched npm run build entry"
    assert match["step"] == 5
    # Builder asks about a command never run — no match
    nomatch = graph._find_recent_verify_output("cargo test")
    assert nomatch is None
    # Verify the user-message renderer surfaces both cases correctly
    msg_match = graph._build_advisor_user_message(
        "task", graph._empty_plan_doc(), "summary", ["e1"], "npm run build")
    assert "Match found at step 5" in msg_match
    assert "Compiled successfully" in msg_match
    msg_no_match = graph._build_advisor_user_message(
        "task", graph._empty_plan_doc(), "summary", ["e1"], "cargo test")
    assert "NO MATCHING SHELL OUTPUT FOUND" in msg_no_match
    print("  PASS — match + no-match paths render correctly")


async def main():
    test_retry_classification()
    await test_retry_succeeds_after_two_failures()
    await test_retry_exhausted_raises()
    await test_non_retryable_raises_immediately()
    await test_schema_mismatch_rejects_resume()
    await test_mark_done_requires_token()
    await test_token_is_single_use()
    await test_verdict_cap_enforced()
    await test_error_cap_separate_from_verdict_cap()
    await test_unparseable_response_counts_as_error_no_token()
    await test_recent_output_match()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
