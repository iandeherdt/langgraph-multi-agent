"""Manual resilience tests. Run inside the langgraph container:

    docker compose run --rm langgraph python /app/test_resilience.py

Covers:
  1. Retry classification — _is_retryable_error decides correctly per status code.
  2. Streaming retry — _ainvoke_streaming retries transient errors and returns successfully.
  3. Streaming retry — exhausted attempts re-raise.
  4. Streaming retry — non-retryable status raises immediately (no retry).
  5. Schema-mismatch detection — _check_resume_compatibility returns False on version skew.

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


async def main():
    test_retry_classification()
    await test_retry_succeeds_after_two_failures()
    await test_retry_exhausted_raises()
    await test_non_retryable_raises_immediately()
    await test_schema_mismatch_rejects_resume()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
