"""Unit tests for the large-prompt spill helper.

Spilling exists because Linux's MAX_ARG_STRLEN (~128 KB) caps any
single argv element and the prompt is passed positionally to
`claude` (the VM0 shape). See server/large_prompts.py for the full
rationale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server import large_prompts as lp
from server.large_prompts import (
    LARGE_PROMPT_THRESHOLD_BYTES,
    delete_session_large_prompts,
    spill_if_large,
)


@pytest.fixture
def spill_root(tmp_path, monkeypatch):
    """Redirect large-prompts storage to a per-test tmpdir.

    `_large_prompts_root` reads `settings.large_prompts_dir` at call
    time, so a setattr is enough — no module reload needed.
    """
    from server.config import settings

    monkeypatch.setattr(settings, "large_prompts_dir", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Threshold behavior
# ---------------------------------------------------------------------------


def test_small_prompt_passes_through_unchanged(spill_root):
    """Anything at or below the threshold is returned as-is, with no
    file written. This is the hot path — must be cheap."""
    prompt = "hello world"
    result = spill_if_large("sess-small", prompt)
    assert result == prompt
    # No session dir should have been created either.
    assert not (spill_root / "sess-small").exists()


def test_prompt_exactly_at_threshold_passes_through(spill_root):
    """The threshold is inclusive: a prompt whose UTF-8 byte length
    equals the cap fits, so no spill. Off-by-one regressions show up
    here."""
    prompt = "x" * LARGE_PROMPT_THRESHOLD_BYTES
    result = spill_if_large("sess-edge", prompt)
    assert result == prompt
    assert not (spill_root / "sess-edge").exists()


def test_oversize_prompt_is_spilled(spill_root):
    """A prompt one byte over the threshold gets spilled to disk and
    the caller receives a pointer message — not the original text."""
    prompt = "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1)
    result = spill_if_large("sess-big", prompt)

    assert result != prompt
    assert "[octopus-large-prompt]" in result
    # The pointer must cite an absolute path the model's Read tool
    # can open without working_dir confusion.
    assert str(spill_root / "sess-big") in result

    # The file on disk holds the FULL original prompt verbatim.
    files = list((spill_root / "sess-big").glob("*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == prompt


def test_pointer_reports_actual_byte_size(spill_root):
    """The model needs to know how big the spilled prompt is so it
    can decide whether to Read in chunks (Read with offset/limit)
    or in one shot."""
    prompt = "ω" * 80_000  # multi-byte chars → byte size differs from char count
    result = spill_if_large("sess-omega", prompt)
    expected_bytes = len(prompt.encode("utf-8"))
    assert f"{expected_bytes:,} bytes" in result


def test_pointer_instructs_model_to_read_in_full(spill_root):
    """The pointer must be unambiguous — the model should not respond
    based on the pointer text alone. Test asserts the explicit
    instruction is present."""
    prompt = "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1)
    result = spill_if_large("sess-instruct", prompt)
    assert "Read that file in full" in result


# ---------------------------------------------------------------------------
# Marker preservation
# ---------------------------------------------------------------------------


def test_bg_task_result_marker_is_preserved_at_front(spill_root):
    """The frontend's auto-badge keys off `[bg-task-result]` as the
    leading marker in the prompt body — must survive spilling so the
    user-visible chip rendering stays correct (see bg_tasks.py)."""
    big_body = "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1)
    prompt = f"[bg-task-result] Background task `abc123` finished.\n\n{big_body}"
    result = spill_if_large("sess-bg", prompt)
    assert result.startswith("[bg-task-result] ")


def test_plain_user_prompt_has_no_artificial_marker(spill_root):
    """If the original prompt had no recognized marker, the pointer
    shouldn't invent one — only `[octopus-large-prompt]` should
    appear, and the file content stays unmarked."""
    prompt = "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1)
    result = spill_if_large("sess-plain", prompt)
    assert not result.startswith("[bg-task-result]")
    assert result.startswith("[octopus-large-prompt]")
    # The spilled file is verbatim — no marker prepended to disk.
    files = list((spill_root / "sess-plain").glob("*.txt"))
    assert files[0].read_text(encoding="utf-8") == prompt


# ---------------------------------------------------------------------------
# Atomicity + safety
# ---------------------------------------------------------------------------


def test_spill_writes_atomically(spill_root, monkeypatch):
    """Implementation writes to a `.tmp` sibling, then renames. Verify
    the temp file is gone after the call — if rename failed, a
    partially-written `.tmp` would be observable, which would mean
    the model could Read truncated content on a crash mid-write."""
    prompt = "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1)
    spill_if_large("sess-atomic", prompt)
    tmp_files = list((spill_root / "sess-atomic").glob("*.tmp"))
    assert tmp_files == []
    final_files = list((spill_root / "sess-atomic").glob("*.txt"))
    assert len(final_files) == 1


def test_invalid_session_id_is_rejected(spill_root):
    """A session id containing path separators could otherwise be
    used to scribble outside the spill root. We surface the bad input
    as ValueError rather than silently writing somewhere unsafe."""
    prompt = "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1)
    with pytest.raises(ValueError):
        spill_if_large("../escape", prompt)
    with pytest.raises(ValueError):
        spill_if_large("ok\\nope", prompt)
    with pytest.raises(ValueError):
        spill_if_large("", prompt)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_delete_session_large_prompts_removes_dir(spill_root):
    """When a session is deleted the spill dir goes with it. Mirrors
    delete_session_attachments behavior so callers can run the two
    side by side without divergent semantics."""
    spill_if_large("sess-cleanup", "x" * (LARGE_PROMPT_THRESHOLD_BYTES + 1))
    assert (spill_root / "sess-cleanup").is_dir()
    delete_session_large_prompts("sess-cleanup")
    assert not (spill_root / "sess-cleanup").exists()


def test_delete_session_large_prompts_is_noop_when_missing(spill_root):
    """Best-effort: deleting a never-spilled session must not raise."""
    delete_session_large_prompts("never-spilled")  # no exception


# ---------------------------------------------------------------------------
# Session-manager integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_hands_backend_pointer_for_huge_prompt(
    spill_root, monkeypatch
):
    """End-to-end at the session_manager boundary: a huge prompt
    persists as the original text in the message row (so chat history
    is faithful to what the user sent), but the backend receives the
    pointer, not the 100 KB blob. Without this, the spill module is
    decoupled from the path that actually triggers E2BIG."""
    from server.harness import HarnessEvent
    from server.database import Database
    from server.session_manager import SessionManager

    mgr = SessionManager()
    db = Database(":memory:")
    await db.initialize()
    try:
        await mgr.initialize(db)
        agent = await db.get_system_agent()
        session = await mgr.create_session(agent["id"], name="Huge")

        received_prompts: list[str] = []

        class RecordingBackend:

            async def start(
                self, prompt, working_dir, resume_id=None, credential=None
            ):
                received_prompts.append(prompt)

            def stream(self):
                async def _gen():
                    yield HarnessEvent(
                        type="session_started", session_id="sid-huge"
                    )
                    yield HarnessEvent(type="text", content="ok")
                    yield HarnessEvent(
                        type="result",
                        session_id="sid-huge",
                        cost=0.0,
                        num_turns=1,
                    )

                return _gen()

            async def stop(self):
                pass

        mgr._make_run = lambda s, agent=None, connectors=None: RecordingBackend()  # type: ignore[method-assign,assignment]

        huge = "Q" * (LARGE_PROMPT_THRESHOLD_BYTES + 50_000)
        async for _ in mgr.send_message(session.id, huge):
            pass

        # Backend got the pointer, NOT the 150 KB original. This is the
        # property that makes the whole feature load-bearing for E2BIG.
        assert len(received_prompts) == 1
        backend_prompt = received_prompts[0]
        assert "[octopus-large-prompt]" in backend_prompt
        assert len(backend_prompt.encode("utf-8")) < LARGE_PROMPT_THRESHOLD_BYTES

        # The DB message row should hold the original prompt verbatim so
        # the UI / transcript shows what the user actually typed, not the
        # spill pointer.
        rows = await db.load_messages(session.id)
        user_rows = [r for r in rows if r["role"] == "user"]
        assert any(r["content"] == huge for r in user_rows), (
            "message persistence should keep the original prompt, "
            "not the spill pointer"
        )
    finally:
        await db.close()
