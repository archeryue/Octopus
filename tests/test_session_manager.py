import asyncio
from typing import Any

import pytest

from server.database import Database
from server.models import SessionStatus
from server.session_manager import SessionManager


@pytest.fixture
async def manager():
    mgr = SessionManager()
    db = Database(":memory:")
    await db.initialize()
    await mgr.initialize(db)
    try:
        yield mgr
    finally:
        # Close the aiosqlite connection so its worker thread exits
        # before the per-test event loop is torn down. A leaked
        # connection's thread later crashes with "Event loop is
        # closed" and pins the pytest process at atexit.
        await db.close()


async def _new(manager, name="S", working_dir=None, credential_id=None, origin="user"):
    """Create a session under the Default Agent (created by migration)."""
    agent = await manager.db.get_system_agent()
    _create = manager.create_session
    return await _create(
        agent["id"], name, working_dir, credential_id=credential_id, origin=origin
    )


@pytest.mark.asyncio
async def test_create_session(manager):
    session = await _new(manager,"Test Session", "/tmp")
    assert session.name == "Test Session"
    assert session.working_dir == "/tmp"
    assert session.status == SessionStatus.idle
    assert len(session.id) == 12
    assert session.id in manager.sessions


@pytest.mark.asyncio
async def test_create_session_default_dir(manager):
    session = await _new(manager,"Default Dir")
    assert session.working_dir == "."


@pytest.mark.asyncio
async def test_list_sessions(manager):
    assert manager.list_sessions() == []
    await _new(manager,"A")
    await _new(manager,"B")
    sessions = manager.list_sessions()
    assert len(sessions) == 2
    names = {s.name for s in sessions}
    assert names == {"A", "B"}


@pytest.mark.asyncio
async def test_get_session(manager):
    session = await _new(manager,"Find Me")
    found = manager.get_session(session.id)
    assert found is session
    assert manager.get_session("nonexistent") is None


@pytest.mark.asyncio
async def test_delete_session(manager):
    session = await _new(manager,"Delete Me")
    sid = session.id
    assert await manager.delete_session(sid) is True
    assert manager.get_session(sid) is None
    assert await manager.delete_session(sid) is False


@pytest.mark.asyncio
async def test_send_message_unknown_session(manager):
    with pytest.raises(ValueError, match="not found"):
        async for _ in manager.send_message("nonexistent", "hello"):
            pass


@pytest.mark.asyncio
async def test_broadcast_registration(manager):
    calls = []

    async def cb(msg):
        calls.append(msg)

    manager.on_broadcast("test", cb)
    assert "test" in manager._broadcast_callbacks

    manager.remove_broadcast("test")
    assert "test" not in manager._broadcast_callbacks


@pytest.mark.asyncio
async def test_create_session_persists_to_db(manager):
    session = await _new(manager,"Persisted", "/home")
    rows = await manager.db.load_sessions()
    assert any(r["id"] == session.id for r in rows)


@pytest.mark.asyncio
async def test_delete_session_removes_from_db(manager):
    session = await _new(manager,"To Delete", "/tmp")
    sid = session.id
    await manager.delete_session(sid)
    rows = await manager.db.load_sessions()
    assert not any(r["id"] == sid for r in rows)


@pytest.mark.asyncio
async def test_initialize_restores_sessions():
    """Create a session with one manager, then load into a fresh manager."""
    db = Database(":memory:")
    await db.initialize()
    try:
        mgr1 = SessionManager()
        await mgr1.initialize(db)
        session = await _new(mgr1, "Restored", "/tmp")
        sid = session.id

        # Create a fresh manager, initialize with the same DB
        mgr2 = SessionManager()
        await mgr2.initialize(db)
        restored = mgr2.get_session(sid)
        assert restored is not None
        assert restored.name == "Restored"
        assert restored.working_dir == "/tmp"
        assert restored.status == SessionStatus.idle
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Message queue + interrupt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_message_queues_when_busy(manager, monkeypatch):
    session = await _new(manager,"Q")
    consumed: list[str] = []
    blocker = asyncio.Event()

    async def stub_consume(session_id: str, queued) -> None:
        consumed.append(queued.prompt)
        if len(consumed) == 1:
            await blocker.wait()

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    events: list[dict] = []

    async def cb(msg: dict) -> None:
        events.append(msg)

    manager.on_broadcast("test", cb)

    await manager.start_message(session.id, "first")
    # Yield so the orchestrator + first stub_consume get a chance to start
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Second start_message should queue rather than fire
    await manager.start_message(session.id, "second")
    assert [qp.prompt for qp in session._pending_queue] == ["second"]

    queued = [e for e in events if e["type"] == "queued"]
    assert len(queued) == 1
    assert queued[0]["content"] == "second"
    assert queued[0]["queue_length"] == 1

    # Release the blocker; orchestrator should drain the queue
    blocker.set()
    await asyncio.wait_for(session._active_task, timeout=2)

    assert consumed == ["first", "second"]
    assert session._pending_queue == []
    assert any(e["type"] == "dequeued" for e in events)


@pytest.mark.asyncio
async def test_interrupt_cancels_current_and_advances_queue(manager, monkeypatch):
    session = await _new(manager,"I")
    started: list[str] = []
    cancelled: list[str] = []

    async def stub_consume(session_id: str, queued) -> None:
        started.append(queued.prompt)
        try:
            # Block forever so interrupt() must cancel us
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(queued.prompt)
            raise

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    await manager.start_message(session.id, "first")
    # Wait until the inner task is scheduled and started
    for _ in range(20):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started == ["first"]

    await manager.start_message(session.id, "second")
    assert [qp.prompt for qp in session._pending_queue] == ["second"]

    ok = await manager.interrupt(session.id)
    assert ok is True

    # Allow the orchestrator to pick up the dequeued prompt
    for _ in range(50):
        if "second" in started:
            break
        await asyncio.sleep(0.01)

    assert started == ["first", "second"]
    assert cancelled == ["first"]
    assert session._pending_queue == []

    # Cleanup: cancel the second so the test doesn't hang
    await manager.interrupt(session.id)
    try:
        await asyncio.wait_for(session._active_task, timeout=1)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_interrupt_twice_in_a_row_each_works(manager, monkeypatch):
    """Reproduces the bug where pressing Esc to interrupt a queued message
    that just started running was a no-op."""
    session = await _new(manager,"DoubleInterrupt")
    started: list[str] = []
    cancelled: list[str] = []

    async def stub_consume(session_id: str, queued) -> None:
        started.append(queued.prompt)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(queued.prompt)
            raise

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    await manager.start_message(session.id, "first")
    for _ in range(50):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started == ["first"]

    await manager.start_message(session.id, "second")
    assert [qp.prompt for qp in session._pending_queue] == ["second"]

    # First interrupt
    assert await manager.interrupt(session.id) is True

    # Wait for the queue to advance and "second" to start
    for _ in range(100):
        if "second" in started:
            break
        await asyncio.sleep(0.01)
    assert started == ["first", "second"]
    assert cancelled == ["first"]

    # Second interrupt — this is the bug repro: must also succeed
    assert await manager.interrupt(session.id) is True

    for _ in range(100):
        if "second" in cancelled:
            break
        await asyncio.sleep(0.01)
    assert cancelled == ["first", "second"]
    assert session._pending_queue == []

    try:
        await asyncio.wait_for(session._active_task, timeout=1)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_interrupt_does_not_wedge_on_slow_backend_stop(manager, monkeypatch):
    """If the backend's stop()/interrupt() hangs, the manager's interrupt()
    must still return promptly (within the timeout) so the WS receive loop
    isn't blocked from processing subsequent interrupts."""
    from server.backends import BackendBase

    session = await _new(manager,"SlowStop")

    async def stub_consume(session_id: str, queued) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    class HangingBackend(BackendBase):
        name = "hanging"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            pass

        def stream(self):
            async def _gen():
                await asyncio.sleep(60)
                yield  # never reached
            return _gen()

        async def stop(self):
            await asyncio.sleep(60)  # would hang interrupt() if not timed out

        async def interrupt(self):
            await asyncio.sleep(60)

    monkeypatch.setattr(manager, "_consume_message", stub_consume)
    await manager.start_message(session.id, "x")
    for _ in range(20):
        if session._inner_task and not session._inner_task.done():
            break
        await asyncio.sleep(0.01)

    # Plant the hanging backend on the session
    session._backend = HangingBackend()  # type: ignore[assignment]

    # interrupt() must return within the backend-interrupt timeout (2s) + a margin
    try:
        ok = await asyncio.wait_for(manager.interrupt(session.id), timeout=4.0)
    except asyncio.TimeoutError:
        pytest.fail("interrupt() blocked on hanging backend — WS would be wedged")

    assert ok is True

    try:
        await asyncio.wait_for(session._active_task, timeout=2)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


@pytest.mark.asyncio
async def test_interrupt_when_idle_returns_false(manager):
    session = await _new(manager,"Idle")
    assert await manager.interrupt(session.id) is False


@pytest.mark.asyncio
async def test_format_answers_handles_select_and_text(manager):
    questions = [
        {"question": "Favorite color?", "options": []},
        {"question": "Notes?", "options": []},
    ]
    answers = [
        {"selected": ["blue"]},
        {"text": "I like teal too"},
    ]
    out = SessionManager._format_answers(questions, answers)
    assert "Favorite color?" in out
    assert "blue" in out
    assert "Notes?" in out
    assert "I like teal too" in out


@pytest.mark.asyncio
async def test_answer_question_unknown_returns_false(manager):
    session = await _new(manager,"UnknownQ")
    assert await manager.answer_question(session.id, "nope", []) is False


@pytest.mark.asyncio
async def test_answer_question_sets_event_and_broadcasts(manager):
    """VM0-shape Q&A: answer_question formats the answers, stores the
    text in `_pending_question_answers`, sets the asyncio.Event that
    wakes the mcp__ask__user long-poll, persists the chat entry, and
    broadcasts the question_answer WS event. The backend is no longer
    involved — AUQ flows entirely through the question state in
    session_manager."""
    from server.session_manager import PendingQuestion

    session = await _new(manager,"Q")
    events: list[dict] = []

    async def cb(msg: dict) -> None:
        events.append(msg)

    manager.on_broadcast("test", cb)

    # Simulate what the new flow does on `mcp__ask__user`:
    #   the ask MCP server's POST → create_pending_question creates
    #   the PendingQuestion + Event. Here we set them up manually so
    #   we can exercise answer_question() in isolation.
    session._pending_questions["q-1"] = PendingQuestion(
        question_id="q-1",
        questions=[{"question": "Pick one", "options": [{"label": "A"}]}],
    )
    session._pending_question_events["q-1"] = asyncio.Event()

    ok = await manager.answer_question(session.id, "q-1", [{"selected": ["A"]}])
    assert ok is True

    # Answer text stored where the long-poll will read it.
    assert session._pending_question_answers["q-1"] == "Q: Pick one\nA: A"
    # Event signalled so the long-poll wakes up.
    assert session._pending_question_events["q-1"].is_set()
    # Pending question cleared, broadcast emitted with the formatted text.
    assert "q-1" not in session._pending_questions
    qa = [e for e in events if e["type"] == "question_answer"]
    assert len(qa) == 1
    assert qa[0]["content"] == "Q: Pick one\nA: A"


@pytest.mark.asyncio
async def test_wait_for_question_answer_unblocks_on_submit(manager):
    """The ask MCP server's HTTP long-poll calls wait_for_question_answer
    which awaits the Event. When the user submits, the wait returns
    the formatted answer text."""
    from server.session_manager import PendingQuestion

    session = await _new(manager,"Wait")
    session._pending_questions["qid"] = PendingQuestion(
        question_id="qid",
        questions=[{"question": "OK?", "options": [{"label": "Y"}]}],
    )
    session._pending_question_events["qid"] = asyncio.Event()

    async def submit_after_delay():
        await asyncio.sleep(0.05)
        await manager.answer_question(session.id, "qid", [{"selected": ["Y"]}])

    asyncio.create_task(submit_after_delay())
    answer = await manager.wait_for_question_answer(
        session.id, "qid", timeout=2.0
    )
    assert answer == "Q: OK?\nA: Y"


@pytest.mark.asyncio
async def test_wait_for_question_answer_returns_none_on_timeout(manager):
    """When the user takes too long for a single poll window, the
    waiter returns None so the MCP server can re-poll."""
    from server.session_manager import PendingQuestion

    session = await _new(manager,"Timeout")
    session._pending_questions["qid"] = PendingQuestion(
        question_id="qid", questions=[{"question": "?", "options": []}]
    )
    session._pending_question_events["qid"] = asyncio.Event()

    answer = await manager.wait_for_question_answer(
        session.id, "qid", timeout=0.1
    )
    assert answer is None


@pytest.mark.asyncio
async def test_unanswered_question_auto_answers_after_timeout(
    manager, monkeypatch
):
    """Session-level auto-answer still works in VM0 shape: after the
    configured timeout, deliver the autonomy-mode text via the same
    Event mechanism, broadcast with auto=True."""
    from server import session_manager as sm
    from server.session_manager import PendingQuestion

    monkeypatch.setattr(sm.settings, "ask_user_question_timeout_seconds", 0.05)

    session = await _new(manager,"AutoQ")
    events: list[dict] = []

    async def cb(msg: dict) -> None:
        events.append(msg)

    manager.on_broadcast("auto", cb)

    session._pending_questions["q-auto"] = PendingQuestion(
        question_id="q-auto",
        questions=[{"question": "What now?", "options": []}],
    )
    session._pending_question_events["q-auto"] = asyncio.Event()
    manager._schedule_question_timeout(session, "q-auto")

    await asyncio.sleep(0.2)

    # Auto-answer text is what the long-poll will read.
    answer = session._pending_question_answers.get("q-auto")
    assert answer is not None
    assert "No human is available" in answer
    assert "risky or irreversible" in answer
    # Event signalled, pending cleared, broadcast carries auto=True.
    assert session._pending_question_events["q-auto"].is_set()
    assert "q-auto" not in session._pending_questions
    auto_events = [
        e for e in events
        if e.get("type") == "question_answer" and e.get("auto") is True
    ]
    assert len(auto_events) == 1


@pytest.mark.asyncio
async def test_manual_answer_cancels_auto_answer_timer(manager, monkeypatch):
    """If the user answers before the timeout, the auto-answer timer
    should be cancelled and never fire — otherwise the long-poll
    would see the autonomy-mode text instead of the user's choice."""
    from server import session_manager as sm
    from server.session_manager import PendingQuestion

    monkeypatch.setattr(sm.settings, "ask_user_question_timeout_seconds", 0.5)

    session = await _new(manager,"ManualBeatsTimer")
    session._pending_questions["q-1"] = PendingQuestion(
        question_id="q-1",
        questions=[{"question": "Pick", "options": [{"label": "A"}]}],
    )
    session._pending_question_events["q-1"] = asyncio.Event()
    manager._schedule_question_timeout(session, "q-1")

    await manager.answer_question(session.id, "q-1", [{"selected": ["A"]}])

    # Wait past the timeout — the auto-answer text must NOT overwrite
    # the user's choice.
    await asyncio.sleep(0.7)
    assert "No human is available" not in session._pending_question_answers["q-1"]


@pytest.mark.asyncio
async def test_event_to_message_content_maps_question_request():
    """The translation layer keeps the persisted shape stable so existing
    UI handling for question_request messages doesn't break."""
    from server.backends import BackendEvent
    from server.session_manager import SessionManager

    ev = BackendEvent(
        type="question_request",
        tool_use_id="q-99",
        tool_input={"questions": [{"question": "X?", "options": []}]},
    )
    msg = SessionManager._event_to_message_content(ev)
    assert msg is not None
    assert msg.type == "question_request"
    assert msg.tool_name == "AskUserQuestion"
    assert msg.tool_use_id == "q-99"


@pytest.mark.asyncio
async def test_resolve_credential_returns_decrypted_secret(manager):
    """When a session has credential_id, _resolve_credential should fetch
    and decrypt the row."""
    from datetime import datetime, timezone
    from server.config import settings
    from server.crypto import encrypt

    now = datetime.now(timezone.utc).isoformat()
    enc = encrypt("sk-ant-secret", settings.auth_token)
    await manager.db.save_credential(
        credential_id="c-1",
        backend="claude-code",
        label="L",
        auth_type="api_key",
        secret_encrypted=enc,
        created_at=now,
    )
    session = await _new(manager,"S", credential_id="c-1")
    cred = await manager._resolve_credential(session)
    assert cred is not None
    assert cred.backend == "claude-code"
    assert cred.auth_type == "api_key"
    assert cred.secret == "sk-ant-secret"


@pytest.mark.asyncio
async def test_resolve_credential_returns_none_when_missing(manager):
    session = await _new(manager,"S", credential_id="ghost")
    cred = await manager._resolve_credential(session)
    assert cred is None


@pytest.mark.asyncio
async def test_resolve_credential_oauth_bundle_returns_oauth_credential(manager):
    """OAuth-bundle credentials (stored as a JSON blob with refresh_token)
    return BackendCredential(auth_type='oauth', secret=access_token)."""
    import json
    import time
    from datetime import datetime, timezone
    from server.config import settings
    from server.crypto import encrypt

    bundle = json.dumps(
        {
            "access_token": "oat-fresh-access",
            "refresh_token": "ort-refresh",
            # Comfortably in the future so no refresh is triggered.
            "expires_at_epoch": time.time() + 3600,
            "scopes": ["user:inference"],
            "token_type": "Bearer",
        }
    )
    enc = encrypt(bundle, settings.auth_token)
    await manager.db.save_credential(
        credential_id="c-oauth",
        backend="claude-code",
        label="Pro/Max",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session = await _new(manager,"S-oauth", credential_id="c-oauth")
    cred = await manager._resolve_credential(session)
    assert cred is not None
    assert cred.auth_type == "oauth"
    assert cred.secret == "oat-fresh-access"


@pytest.mark.asyncio
async def test_resolve_credential_refreshes_expired_oauth_token(manager, monkeypatch):
    """When the stored access_token is past expiry, the resolver should
    call the provider's refresh endpoint, persist the new bundle, and
    hand back the fresh access_token."""
    import json
    import time
    from datetime import datetime, timezone
    from server import oauth_providers as op
    from server.config import settings
    from server.crypto import decrypt, encrypt
    from server.oauth_providers import OAuthTokenSet

    expired_bundle = json.dumps(
        {
            "access_token": "oat-expired",
            "refresh_token": "ort-still-valid",
            # 1 minute ago — well past leeway
            "expires_at_epoch": time.time() - 60,
            "scopes": ["user:inference"],
            "token_type": "Bearer",
        }
    )
    enc = encrypt(expired_bundle, settings.auth_token)
    await manager.db.save_credential(
        credential_id="c-stale",
        backend="claude-code",
        label="Stale",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    captured_refresh: list[str] = []

    async def fake_refresh(refresh_token):
        captured_refresh.append(refresh_token)
        return OAuthTokenSet(
            access_token="oat-brand-new",
            refresh_token="ort-still-valid",
            expires_at_epoch=time.time() + 3600,
            scopes=["user:inference"],
        )

    provider = op.get_provider("claude-code")
    monkeypatch.setattr(provider, "refresh_access_token", fake_refresh)

    session = await _new(manager,"S-stale", credential_id="c-stale")
    cred = await manager._resolve_credential(session)

    assert captured_refresh == ["ort-still-valid"]
    assert cred is not None
    assert cred.auth_type == "oauth"
    assert cred.secret == "oat-brand-new"

    # The new bundle was persisted: a second resolve should find a fresh
    # row (and NOT refresh again, since the new bundle is not expired).
    captured_refresh.clear()
    cred2 = await manager._resolve_credential(session)
    assert cred2 is not None
    assert cred2.secret == "oat-brand-new"
    assert captured_refresh == []  # no second refresh

    # And the stored bundle decrypts to the new access_token.
    row = await manager.db.get_credential("c-stale")
    new_bundle = json.loads(decrypt(row["secret_encrypted"], settings.auth_token))
    assert new_bundle["access_token"] == "oat-brand-new"
    assert row.get("needs_reconnect") is False


@pytest.mark.asyncio
async def test_resolve_credential_marks_needs_reconnect_on_refresh_failure(
    manager, monkeypatch
):
    """Refresh failure → credential is marked needs_reconnect with a
    typed error code, and the resolver returns None (so the backend
    falls back to no credential rather than firing a broken request)."""
    import json
    import time
    from datetime import datetime, timezone
    from server import oauth_providers as op
    from server.config import settings
    from server.crypto import encrypt

    expired_bundle = json.dumps(
        {
            "access_token": "oat-expired",
            "refresh_token": "ort-dead",
            "expires_at_epoch": time.time() - 60,
            "scopes": [],
            "token_type": "Bearer",
        }
    )
    enc = encrypt(expired_bundle, settings.auth_token)
    await manager.db.save_credential(
        credential_id="c-dead",
        backend="claude-code",
        label="Dead",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    async def fake_refresh(refresh_token):
        # The provider raises RuntimeError(...) on a 400 from the token endpoint
        raise RuntimeError(
            "refresh endpoint returned 400: invalid_grant — refresh token expired"
        )

    provider = op.get_provider("claude-code")
    monkeypatch.setattr(provider, "refresh_access_token", fake_refresh)

    session = await _new(manager,"S-dead", credential_id="c-dead")
    cred = await manager._resolve_credential(session)
    assert cred is None

    row = await manager.db.get_credential("c-dead")
    assert row.get("needs_reconnect") is True
    assert row.get("last_refresh_error_code") == "refresh_token_expired"

    # A subsequent resolve sees needs_reconnect and returns None without
    # retrying the refresh.
    cred2 = await manager._resolve_credential(session)
    assert cred2 is None


@pytest.mark.asyncio
async def test_oauth_credential_env_var_reaches_subprocess(manager, monkeypatch):
    """End-to-end: OAuth-bundle credential → resolver decrypts/refreshes →
    backend build_args lands the access_token in CLAUDE_CODE_OAUTH_TOKEN
    on the subprocess env. Mirrors the existing ANTHROPIC_API_KEY test."""
    import json
    import time
    from datetime import datetime, timezone
    from server.config import settings
    from server.crypto import encrypt

    bundle = json.dumps(
        {
            "access_token": "oat-runtime-token",
            "refresh_token": "ort-x",
            "expires_at_epoch": time.time() + 3600,
            "scopes": ["user:inference"],
            "token_type": "Bearer",
        }
    )
    enc = encrypt(bundle, settings.auth_token)
    await manager.db.save_credential(
        credential_id="c-env-oauth",
        backend="claude-code",
        label="EnvOAuth",
        auth_type="oauth",
        secret_encrypted=enc,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session = await _new(manager,
        "EnvSessionOAuth", credential_id="c-env-oauth"
    )

    cred = await manager._resolve_credential(session)
    assert cred is not None
    backend = manager._make_backend(session)
    _, kwargs = backend.build_args(
        "prompt", session.working_dir, None, credential=cred
    )
    env = kwargs.get("env") or {}
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == "oat-runtime-token"
    # And we don't accidentally set both — that would confuse the CLI.
    assert "ANTHROPIC_API_KEY" not in {
        k for k in env.keys() if env[k] == "oat-runtime-token"
    }


@pytest.mark.asyncio
async def test_credential_env_var_reaches_spawned_subprocess(manager):
    """End-to-end-ish: when a session has a credential, the *decrypted*
    secret really lands in the env dict that would be passed to
    asyncio.create_subprocess_exec.

    Covers the chain: DB row → _resolve_credential → BackendCredential →
    SessionManager._make_backend → ClaudeCodeBackend.build_args.
    """
    from datetime import datetime, timezone
    from server.config import settings
    from server.crypto import encrypt

    # Seed a credential
    enc = encrypt("sk-real-secret", settings.auth_token)
    await manager.db.save_credential(
        credential_id="c-env",
        backend="claude-code",
        label="EnvTest",
        auth_type="api_key",
        secret_encrypted=enc,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    session = await _new(manager,"EnvSession", credential_id="c-env")

    # 1. Resolve through the session_manager pipeline
    cred = await manager._resolve_credential(session)
    assert cred is not None and cred.secret == "sk-real-secret"

    # 2. The backend factory the session_manager would use must then turn
    # that credential into a real env on the subprocess invocation.
    backend = manager._make_backend(session)
    argv, kwargs = backend.build_args(
        "prompt", session.working_dir, None, credential=cred
    )
    env = kwargs.get("env") or {}
    assert env.get("ANTHROPIC_API_KEY") == "sk-real-secret", (
        f"decrypted secret didn't make it to subprocess env: {env.get('ANTHROPIC_API_KEY')!r}"
    )

    # argv should be the real claude CLI in VM0 shape — positional
    # argv prompt, --dangerously-skip-permissions instead of the old
    # --permission-prompt-tool=stdio path. The migration away from
    # --input-format=stream-json was the whole point of the refactor
    # (see docs/2026-05-18-bg-pipeline-hardening.md §2).
    argv_str = " ".join(str(a) for a in argv)
    assert "claude" in argv_str
    assert "--dangerously-skip-permissions" in argv_str
    assert "--input-format=stream-json" not in argv_str

    # And a sanity check on the *negative* path: a session with no
    # credential must NOT inject one (unless the parent shell already had).
    bare_session = await _new(manager,"Bare")
    bare_backend = manager._make_backend(bare_session)
    _, bare_kwargs = bare_backend.build_args(
        "p", bare_session.working_dir, None, credential=None
    )
    import os as _os
    assert (bare_kwargs.get("env") or {}).get("ANTHROPIC_API_KEY") == _os.environ.get(
        "ANTHROPIC_API_KEY"
    )


@pytest.mark.asyncio
async def test_make_backend_applies_agent_config(manager):
    """_make_backend reads the agent's system prompt / model / MCP set /
    tool allow-deny and translates them to CLI args (agent-refactor.md §5.2)."""
    import json as _json
    import uuid as _uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    aid = _uuid.uuid4().hex[:12]
    await manager.db.save_agent(
        agent_id=aid,
        name="Configured",
        created_at=now,
        updated_at=now,
        system_prompt="You are a pirate.",
        model="claude-opus-4-7",
        mcp_servers=["ask"],
        tool_allow="Read\nGrep",
        tool_deny="Bash",
    )
    session = await manager.create_session(aid, name="S")
    agent = await manager.db.get_agent(aid)
    backend = manager._make_backend(session, agent)
    argv, _ = backend.build_args("hi", session.working_dir, None)

    # model
    assert "--model" in argv and "claude-opus-4-7" in argv
    # agent persona is appended ahead of the Octopus tools section
    ap = argv[argv.index("--append-system-prompt") + 1]
    assert "You are a pirate." in ap
    assert "Octopus in-app tools" in ap
    # deny = always-on AskUserQuestion + agent deny; allow = agent allow
    dis = argv[argv.index("--disallowedTools") + 1]
    assert "AskUserQuestion" in dis and "Bash" in dis
    allow = argv[argv.index("--allowedTools") + 1]
    assert "Read" in allow and "Grep" in allow
    # only the selected MCP server is registered
    cfg = _json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(cfg["mcpServers"].keys()) == {"ask"}


@pytest.mark.asyncio
async def test_make_backend_dispatches_on_backend(manager):
    """_make_backend returns CodexBackend for backend='codex' and
    ClaudeCodeBackend for 'claude-code' (codex-backend.md §5.5). Codex must
    not inherit the Claude premature-exit recovery."""
    from server.backends import ClaudeCodeBackend, CodexBackend

    agent = await manager.db.get_system_agent()
    claude_s = await manager.create_session(agent["id"], name="C", backend="claude-code")
    codex_s = await manager.create_session(agent["id"], name="X", backend="codex")

    assert isinstance(manager._make_backend(claude_s, agent), ClaudeCodeBackend)
    cx = manager._make_backend(codex_s, agent)
    assert isinstance(cx, CodexBackend)
    assert cx.wants_premature_exit_recovery is False


@pytest.mark.asyncio
async def test_run_backend_translates_events_end_to_end(manager):
    """Stub the backend to produce a sequence of events and verify
    _run_backend translates each one to the expected WS message shape."""
    from server.backends import BackendBase, BackendEvent

    session = await _new(manager,"E2E")

    events_to_emit = [
        BackendEvent(type="text", content="hello"),
        BackendEvent(
            type="tool_use",
            tool_name="Bash",
            tool_input={"command": "ls"},
            tool_use_id="t1",
        ),
        BackendEvent(
            type="tool_result", content="out", tool_use_id="t1", is_error=False
        ),
        BackendEvent(
            type="result", session_id="claude-sid-1", cost=0.01, num_turns=1
        ),
    ]

    class ScriptedBackend(BackendBase):
        name = "scripted"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            pass

        def stream(self):
            async def _gen():
                for e in events_to_emit:
                    yield e
            return _gen()

        async def stop(self):
            pass

    # Patch the factory so _run_backend uses our scripted backend
    def fake_factory(s, agent=None, connectors=None):
        return ScriptedBackend()

    manager._make_backend = fake_factory  # type: ignore[method-assign]

    ws_msgs = [m async for m in manager._run_backend(session, "go")]
    types = [m["type"] for m in ws_msgs]
    assert types == ["assistant_text", "tool_use", "tool_result", "result"]
    assert ws_msgs[0]["content"] == "hello"
    assert ws_msgs[1]["tool"] == "Bash"
    assert ws_msgs[2]["output"] == "out"
    assert ws_msgs[3]["claude_session_id"] == "claude-sid-1"

    # Resume id was persisted
    assert session.claude_session_id == "claude-sid-1"


# ---------------------------------------------------------------------------
# Auto-respawn on CLI premature exit
# (post-mortem in docs/2026-05-18-bg-pipeline-hardening.md §2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_backend_auto_respawns_on_premature_exit_after_tool(manager):
    """If the CLI exits after a tool roundtrip without ever emitting a
    `result` event, _run_backend should respawn the backend exactly
    once with prompt='continue' and the captured resume id, then
    surface the events from the recovery turn."""
    from server.backends import BackendBase, BackendEvent

    session = await _new(manager,"Recovery")

    # Invocation 1: init → tool_use → tool_result, then CLI dies
    # (no `result`). Invocation 2: text → result. The model "finishes"
    # what it owed us after the continue nudge.
    invocations: list[dict[str, Any]] = []

    class FlakyBackend(BackendBase):
        name = "flaky"
        wants_premature_exit_recovery = True

        def __init__(self, events: list[BackendEvent]) -> None:
            self._events = events
            self.started_with: dict[str, Any] | None = None

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            self.started_with = {
                "prompt": prompt,
                "working_dir": working_dir,
                "resume_id": resume_id,
            }
            invocations.append(self.started_with)

        def stream(self):
            async def _gen():
                for e in self._events:
                    yield e
            return _gen()

        async def stop(self):
            pass

    backends_iter = iter([
        FlakyBackend([
            BackendEvent(type="session_started", session_id="claude-sid-recover"),
            BackendEvent(
                type="tool_use",
                tool_name="Read",
                tool_input={"file_path": "/big.md"},
                tool_use_id="tu1",
            ),
            BackendEvent(
                type="tool_result", content="...", tool_use_id="tu1", is_error=False
            ),
            # No `result` — this is the bug.
        ]),
        FlakyBackend([
            BackendEvent(type="session_started", session_id="claude-sid-recover"),
            BackendEvent(type="text", content="continuing where I left off"),
            BackendEvent(
                type="result",
                session_id="claude-sid-recover",
                cost=0.02,
                num_turns=2,
            ),
        ]),
    ])

    manager._make_backend = lambda s, agent=None, connectors=None: next(backends_iter)  # type: ignore[method-assign,assignment]

    ws_msgs: list[dict[str, Any]] = [m async for m in manager._run_backend(session, "go")]
    types = [m["type"] for m in ws_msgs]

    # First invocation's events, then a recovery marker, then the
    # second invocation's events. session_started is internal-only and
    # does not appear in the broadcast stream.
    assert types == ["tool_use", "tool_result", "error", "assistant_text", "result"]
    assert ws_msgs[2]["message"] == "(auto-resumed after CLI exited mid-turn)"
    assert ws_msgs[3]["content"] == "continuing where I left off"
    assert ws_msgs[4]["claude_session_id"] == "claude-sid-recover"

    # Exactly two CLI invocations, the second with prompt="continue"
    # and the resume id captured from the first invocation's init.
    assert len(invocations) == 2
    assert invocations[0]["prompt"] == "go"
    assert invocations[0]["resume_id"] is None
    assert invocations[1]["prompt"] == "continue"
    assert invocations[1]["resume_id"] == "claude-sid-recover"
    assert session.claude_session_id == "claude-sid-recover"


@pytest.mark.asyncio
async def test_run_backend_bounds_recovery_to_single_retry(manager):
    """If the second CLI invocation also exits prematurely after a
    tool roundtrip, give up rather than loop forever. The retry
    budget is one — after that the turn ends without a `result`."""
    from server.backends import BackendBase, BackendEvent

    session = await _new(manager,"BoundedRecovery")

    invocations: list[dict[str, Any]] = []

    def make_flaky_events() -> list[BackendEvent]:
        return [
            BackendEvent(type="session_started", session_id="claude-sid-stuck"),
            BackendEvent(
                type="tool_use",
                tool_name="Read",
                tool_input={"file_path": "/big.md"},
                tool_use_id="tu",
            ),
            BackendEvent(
                type="tool_result", content="...", tool_use_id="tu", is_error=False
            ),
            # No `result` — bug fires every time.
        ]

    class AlwaysFlakyBackend(BackendBase):
        name = "always-flaky"
        wants_premature_exit_recovery = True

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            invocations.append({"prompt": prompt, "resume_id": resume_id})

        def stream(self):
            async def _gen():
                for e in make_flaky_events():
                    yield e
            return _gen()

        async def stop(self):
            pass

    manager._make_backend = lambda s, agent=None, connectors=None: AlwaysFlakyBackend()  # type: ignore[method-assign,assignment]

    ws_msgs: list[dict[str, Any]] = [m async for m in manager._run_backend(session, "go")]

    # Exactly 2 invocations: original + 1 retry. No third attempt.
    assert len(invocations) == 2
    assert invocations[0]["prompt"] == "go"
    assert invocations[1]["prompt"] == "continue"

    # Events from both invocations are surfaced, with the recovery
    # marker between them. No final `result` event since the recovery
    # also failed — the turn just ends.
    types = [m["type"] for m in ws_msgs]
    assert types == ["tool_use", "tool_result", "error", "tool_use", "tool_result"]
    assert ws_msgs[2]["message"] == "(auto-resumed after CLI exited mid-turn)"


@pytest.mark.asyncio
async def test_run_backend_does_not_respawn_on_clean_exit(manager):
    """A turn that ends with a `result` event is healthy — no
    recovery should fire even if it included tool calls."""
    from server.backends import BackendBase, BackendEvent

    session = await _new(manager,"CleanExit")

    invocations: list[str] = []

    class CleanBackend(BackendBase):
        name = "clean"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            invocations.append(prompt)

        def stream(self):
            async def _gen():
                yield BackendEvent(type="session_started", session_id="sid-clean")
                yield BackendEvent(
                    type="tool_use",
                    tool_name="Read",
                    tool_input={"file_path": "/x"},
                    tool_use_id="t",
                )
                yield BackendEvent(
                    type="tool_result", content="ok", tool_use_id="t", is_error=False
                )
                yield BackendEvent(type="text", content="done")
                yield BackendEvent(
                    type="result", session_id="sid-clean", cost=0.01, num_turns=1
                )
            return _gen()

        async def stop(self):
            pass

    manager._make_backend = lambda s, agent=None, connectors=None: CleanBackend()  # type: ignore[method-assign,assignment]

    _ = [m async for m in manager._run_backend(session, "go")]
    assert invocations == ["go"]  # exactly one — no retry


@pytest.mark.asyncio
async def test_run_backend_does_not_respawn_when_no_tool_use(manager):
    """If the CLI dies without ever emitting a tool_use, that's not
    the documented bug — could be auth failure, network drop, etc.
    Don't retry; surface the incomplete turn as-is."""
    from server.backends import BackendBase, BackendEvent

    session = await _new(manager,"DiesEarly")
    invocations: list[str] = []

    class CrashEarlyBackend(BackendBase):
        name = "crash-early"

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            invocations.append(prompt)

        def stream(self):
            async def _gen():
                yield BackendEvent(type="session_started", session_id="sid-early")
                # No tool_use. No result. CLI just died.
            return _gen()

        async def stop(self):
            pass

    manager._make_backend = lambda s, agent=None, connectors=None: CrashEarlyBackend()  # type: ignore[method-assign,assignment]

    _ = [m async for m in manager._run_backend(session, "go")]
    assert invocations == ["go"]  # no retry — not the bug we recover from


@pytest.mark.asyncio
async def test_delete_session_clears_queue(manager, monkeypatch):
    session = await _new(manager,"Del")
    blocker = asyncio.Event()

    async def stub_consume(session_id: str, queued) -> None:
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(manager, "_consume_message", stub_consume)

    await manager.start_message(session.id, "first")
    await asyncio.sleep(0)
    await manager.start_message(session.id, "second")
    assert [qp.prompt for qp in session._pending_queue] == ["second"]

    await manager.delete_session(session.id)
    assert session._pending_queue == []
    assert session._inner_task is None or session._inner_task.cancelled() or session._inner_task.done()


# ---------------------------------------------------------------------------
# /archive feature — hide old history, fresh session with same settings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_creates_new_session_with_same_settings(manager):
    old = await _new(manager,
        name="Work",
        working_dir="/tmp/work",
        credential_id="c-1",
    )
    # Simulate prior conversation: 3 persisted messages, a resume id.
    old._message_count = 3
    old.claude_session_id = "claude-abc"
    await manager.db.update_session_field(
        old.id, claude_session_id="claude-abc"
    )

    new = await manager.archive_session(old.id)

    assert new.id != old.id
    assert new.name == "Work"
    assert new.working_dir == "/tmp/work"
    assert new.credential_id == "c-1"
    # Brand-new conversation — no resume id, no message history.
    assert new.claude_session_id is None
    assert new._message_count == 0


@pytest.mark.asyncio
async def test_archive_hides_old_session_from_list_but_keeps_db_row(manager):
    old = await _new(manager,name="Hide Me", working_dir="/tmp")
    new = await manager.archive_session(old.id)

    listed = [s.id for s in manager.list_sessions()]
    assert old.id not in listed
    assert new.id in listed

    # DB row still present (archived=1), available via include_archived.
    all_rows = await manager.db.load_sessions(include_archived=True)
    archived_ids = [r["id"] for r in all_rows if r["archived"]]
    assert old.id in archived_ids


@pytest.mark.asyncio
async def test_archive_leaves_agent_schedules_and_nulls_bridge_sticky(manager):
    """Schedule/bridge *ownership* is agent-scoped (agent-refactor.md §5.2), so
    archiving a session doesn't change a schedule's owning agent (the schedule
    here has no origin session, so it isn't repointed either — that path is
    covered by test_archive_repoints_origin_session_schedules). The only
    bridge-aware step: a sticky pointer at the archived session is nulled."""
    old = await _new(manager, name="Auto", working_dir="/tmp")
    agent_id = old.agent_id
    await manager.db.save_schedule(
        schedule_id="s-1",
        agent_id=agent_id,
        name="ping",
        prompt="hi",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await manager.db.save_bridge_mapping(
        platform="telegram", chat_id="42", agent_id=agent_id, session_id=old.id
    )

    new = await manager.archive_session(old.id)

    # Schedule still owned by the same agent, untouched.
    schedules = await manager.db.load_schedules()
    assert schedules[0]["agent_id"] == agent_id

    # Bridge keeps its agent binding; the sticky session pointer is nulled.
    bridges = await manager.db.load_bridge_mappings()
    assert bridges[0]["agent_id"] == agent_id
    assert bridges[0]["session_id"] is None
    # The replacement thread is under the same agent.
    assert new.agent_id == agent_id


@pytest.mark.asyncio
async def test_archive_repoints_origin_session_schedules(manager):
    """A schedule created from a session (origin_session_id == that session)
    follows the live successor when the session is archived: the DB row is
    repointed onto the new session and the live job is re-registered."""
    old = await _new(manager, name="Origin", working_dir="/tmp")
    agent_id = old.agent_id

    rescheduled: list[dict] = []

    class FakeRunner:
        async def reschedule(self, row):
            rescheduled.append(row)

    manager.set_schedule_runner(FakeRunner())

    await manager.db.save_schedule(
        schedule_id="s-origin",
        agent_id=agent_id,
        name="digest",
        prompt="summarize",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=old.id,
    )
    # An unrelated schedule (no origin) must be left alone.
    await manager.db.save_schedule(
        schedule_id="s-other",
        agent_id=agent_id,
        name="other",
        prompt="ping",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
    )

    new = await manager.archive_session(old.id)

    rows = {r["id"]: r for r in await manager.db.load_schedules()}
    # The origin-anchored schedule now points at the successor session.
    assert rows["s-origin"]["origin_session_id"] == new.id
    # The unrelated schedule is untouched.
    assert rows["s-other"]["origin_session_id"] is None
    # Only the repointed schedule was re-registered, with the new origin.
    assert [r["id"] for r in rescheduled] == ["s-origin"]
    assert rescheduled[0]["origin_session_id"] == new.id


@pytest.mark.asyncio
async def test_archive_repoints_origin_schedules_without_runner(manager):
    """The DB repoint happens even if no ScheduleRunner is wired (e.g. a
    headless context) — only the live re-register is skipped."""
    old = await _new(manager, name="Origin2", working_dir="/tmp")
    await manager.db.save_schedule(
        schedule_id="s-db-only",
        agent_id=old.agent_id,
        name="digest",
        prompt="summarize",
        interval_seconds=300,
        created_at="2026-01-01T00:00:00+00:00",
        origin_session_id=old.id,
    )

    new = await manager.archive_session(old.id)

    rows = await manager.db.load_schedules()
    assert rows[0]["origin_session_id"] == new.id


@pytest.mark.asyncio
async def test_archive_unknown_session_raises(manager):
    with pytest.raises(ValueError):
        await manager.archive_session("does-not-exist")


@pytest.mark.asyncio
async def test_archive_broadcasts_session_archived_event(manager):
    received: list[dict] = []
    manager.on_broadcast("test", lambda m: asyncio.sleep(0, result=received.append(m)))

    old = await _new(manager,name="X", working_dir="/tmp")
    new = await manager.archive_session(old.id)

    archived_evts = [m for m in received if m.get("type") == "session_archived"]
    assert len(archived_evts) == 1
    assert archived_evts[0]["old_session_id"] == old.id
    assert archived_evts[0]["new_session_id"] == new.id
    assert archived_evts[0]["name"] == "X"
