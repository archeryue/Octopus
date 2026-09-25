"""An application asking a REAL agent a question (app-agent-access.md).

The unit suite proves the plumbing against a fake harness. This proves the
thing the feature actually promises: a running app hands a message plus some
context to one of the user's agents, a real `claude` turn happens in the app's
data directory, and the answer comes back as text the app can render.

Costs a real API call; auto-skipped when the CLI isn't installed or signed in.
"""

from __future__ import annotations

import glob
import os
import uuid
from datetime import datetime, timezone

import pytest

for _d in [
    os.path.expanduser("~/.local/bin"),
    "/usr/local/bin",
    *sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin"))),
]:
    if _d and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")


from server.agent_manager import AgentManager  # noqa: E402
from server.app_agent import AppAgentManager, ORIGIN_APP  # noqa: E402
from server.applications import ApplicationManager, data_dir_for  # noqa: E402
from server.config import settings  # noqa: E402
from server.database import Database  # noqa: E402
from server.session_manager import SessionManager  # noqa: E402

pytestmark = pytest.mark.real

# A word the model would never produce on its own, so "it answered" can't be
# confused with "something echoed the prompt".
CODEWORD = "PERIWINKLE7731"


@pytest.mark.asyncio
@pytest.mark.real_claude
async def test_an_app_asks_a_real_agent_about_its_context(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "applications_dir", str(tmp_path / "applications"))
    db = Database(":memory:")
    await db.initialize()
    try:
        sessions = SessionManager()
        await sessions.initialize(db)
        apps = ApplicationManager()
        apps.bind(session_mgr=sessions, db=db)
        agent_api = AppAgentManager()
        agent_api.bind(session_mgr=sessions, db=db, app_mgr=apps)

        agent = await AgentManager(db).create_agent(name=f"Reader {uuid.uuid4().hex[:6]}")
        # The row directly, not create_application: a build turn would cost a
        # second real call and has nothing to do with what's under test.
        app_id = uuid.uuid4().hex[:12]
        app_dir = str(tmp_path / "applications" / "smartreader")
        os.makedirs(app_dir, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        await db.save_application(
            app_id=app_id,
            name="SmartReader",
            description="Reads a document and discusses it",
            icon=None,
            agent_id=agent["id"],
            session_id=None,
            app_dir=app_dir,
            entrypoint="index.html",
            status="ready",
            created_at=now,
            updated_at=now,
        )
        row = await db.get_application(app_id)

        out = await agent_api.ask(
            row,
            message=(
                "What is the codeword in the document? Reply with the codeword "
                "alone and nothing else."
            ),
            context=f"# Field notes\n\nThe agreed codeword is {CODEWORD}.\n",
        )

        assert CODEWORD in out["reply"], out
        # It happened in a real conversation session owned by the app, in the
        # app's data directory.
        session = sessions.get_session(out["conversation_id"])
        assert session.origin == ORIGIN_APP
        assert session.app_id == app_id
        assert session.working_dir == data_dir_for(app_dir)

        # And it's a conversation: the second turn resumes the first.
        follow_up = await agent_api.ask(
            row,
            message="Repeat that codeword once more, alone.",
            conversation_id=out["conversation_id"],
        )
        assert CODEWORD in follow_up["reply"], follow_up
        assert follow_up["conversation_id"] == out["conversation_id"]
        assert len(agent_api.list_conversations(app_id)) == 1

        agent_api.shutdown()
        apps.shutdown()
        await sessions.stop_all_held_processes()
    finally:
        await db.close()
