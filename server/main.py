import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Clear this so the `claude` CLI subprocess doesn't think it's nested
# inside another Claude Code session (which would change its behavior).
os.environ.pop("CLAUDECODE", None)

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth import verify_token

from .bg_tasks import bg_task_manager
from .delegations import delegation_manager
from .research import research_manager
from .bridges.manager import BridgeManager
from .config import settings
from .tunnel import CloudflareTunnel
from .database import Database
from .notifiers import notifier_manager
from .agent_manager import AgentManager
from .app_backends import backend_supervisor
from .app_agent import app_agent_manager
from .applications import application_manager
from .connector_manager import ConnectorManager
from .routers import agents, applications as applications_router, attachments, auth as auth_router, bg_tasks as bg_tasks_router, connectors, credentials, delegations as delegations_router, files, notifiers, questions, research as research_router, schedules, sessions, ws
from .scheduler import ScheduleRunner
from .session_manager import session_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    await db.initialize()
    await session_manager.initialize(db)

    # Initialize bridge manager
    bridge_manager = BridgeManager(session_manager, db)
    await bridge_manager.initialize()
    await bridge_manager.register_broadcast()

    if settings.telegram_bot_token:
        from .bridges.telegram import TelegramBridge

        telegram = TelegramBridge(
            bridge_manager,
            token=settings.telegram_bot_token,
            allowed_chat_ids=settings.telegram_allowed_chat_ids or None,
            api_base_url=settings.telegram_api_base_url,
        )
        bridge_manager.register_bridge(telegram)

    await bridge_manager.start_all()
    app.state.bridge_manager = bridge_manager

    # Initialize scheduler
    schedule_runner = ScheduleRunner(session_manager, db)
    await schedule_runner.initialize()
    app.state.schedule_runner = schedule_runner
    session_manager.set_schedule_runner(schedule_runner)
    schedules._db = db
    schedules._runner = schedule_runner
    agents.set_manager(AgentManager(db))
    connectors.set_manager(ConnectorManager(db))
    applications_router.set_manager(application_manager)
    auth_router.set_db(db)
    credentials.set_db(db)
    notifiers.set_db(db)
    notifier_manager.set_db(db)
    session_manager.set_notifier_manager(notifier_manager)
    await notifier_manager.load()

    # Bg task worker — lives in this FastAPI process so spawned
    # subprocesses survive any per-turn `claude --print` lifetime. The
    # deliver callback synthesizes a user message into the session; the
    # broadcast callback pushes status events to all WS clients.
    bg_task_manager.bind(
        db=db,
        deliver_cb=session_manager.deliver_bg_result,
        broadcast_cb=session_manager._broadcast,
    )
    await bg_task_manager.start()

    # Agent-to-agent delegations (agent-collaboration.md). Subscribes to
    # the session-manager broadcast bus and routes child-session
    # replies/errors back into the parent session as injected turns.
    delegation_manager.bind(session_mgr=session_manager, db=db)

    # Applications (applications.md). Subscribes to the session broadcast bus
    # so a build session's turns drive each application's building/ready/failed
    # status.
    application_manager.bind(session_mgr=session_manager, db=db)
    # Pick up icons for applications that already exist. Discovery otherwise
    # only runs after a build, so an app that already ships a logo would keep
    # showing the generic fallback until someone rebuilt it.
    await application_manager.refresh_icons()
    # Applications with a backend (application-backends.md): stop the ones
    # nobody is using, and never leave one running past our own exit.
    backend_supervisor.start_reaper()
    # Applications talking to agents (app-agent-access.md). Also a bus
    # subscriber: it turns a conversation session's events into the small
    # vocabulary an app consumes.
    app_agent_manager.bind(
        session_mgr=session_manager, db=db, app_mgr=application_manager
    )

    # Native deep research (native-deep-research.md). Tracks research jobs as
    # async tasks; injects the final report back into the session.
    research_manager.bind(session_mgr=session_manager, db=db)
    await research_manager.recover_interrupted()

    # Start Cloudflare Tunnel if enabled
    tunnel: CloudflareTunnel | None = None
    if settings.enable_tunnel:
        tunnel = CloudflareTunnel(port=settings.port)
        url = await tunnel.start()
        if url:
            print("\n" + "=" * 60)
            print(f"  Tunnel URL: {url}")
            print("=" * 60 + "\n")
            logger.info("Cloudflare Tunnel active: %s", url)

    # Idle-process reaper (inline-steering.md §7): a session keeps its CLI
    # process after a turn so the next one skips the ~1.5s spawn, and this
    # drops the ones that stop earning their ~255MB.
    session_manager.start_reaper()

    yield

    await session_manager.stop_reaper()
    await session_manager.stop_all_held_processes()
    await backend_supervisor.shutdown()

    if tunnel:
        await tunnel.stop()

    # Clean up any in-flight OAuth login subprocesses before we tear down DB.
    from .oauth_login import oauth_login_manager
    await oauth_login_manager.shutdown()
    from .codex_login import codex_login_manager
    await codex_login_manager.shutdown()

    await bg_task_manager.shutdown()
    app_agent_manager.shutdown()
    application_manager.shutdown()
    delegation_manager.shutdown()
    await schedule_runner.shutdown()
    await bridge_manager.stop_all()
    await bridge_manager.unregister_broadcast()
    await db.close()


app = FastAPI(title="Octopus", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _no_store_on_missing(request, call_next):
    """Never let a 404 be cached.

    We send no `Cache-Control` on a 404, so a CDN in front of us is free to
    invent one — Cloudflare caches error responses for static-looking paths for
    four hours. The consequence is nasty and non-obvious: the FIRST request for
    an asset that doesn't exist yet poisons that URL long after the file lands,
    so a newly added icon or script is invisible to everyone behind the edge
    while the origin serves it perfectly. Cost real debugging time once; this
    makes it structurally impossible.
    """
    response = await call_next(request)
    if response.status_code == 404:
        response.headers["Cache-Control"] = "no-store"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router)
app.include_router(applications_router.router)
# /apps/{id}/… — the application itself. Registered before the SPA catch-all
# mount so it wins the path (applications.md §3).
app.include_router(applications_router.static_router)
app.include_router(sessions.router)
app.include_router(attachments.router)
app.include_router(files.router)
app.include_router(bg_tasks_router.router)
app.include_router(delegations_router.router)
app.include_router(research_router.router)
app.include_router(questions.router)
app.include_router(schedules.router)
app.include_router(credentials.router)
app.include_router(auth_router.router)
app.include_router(connectors.router)
app.include_router(connectors.agent_router)
app.include_router(notifiers.router)
app.include_router(ws.router)


@app.get("/api/backends")
async def list_backends(_: str = Depends(verify_token)):
    """Which AI backends are usable on this host (codex-backend.md §6.1).
    A harness kind appears only when its CLI resolves on PATH. `claude-code`
    is always listed (the default) even if not yet installed, matching the
    historical contract."""
    from .harness import available_backends

    available = available_backends()
    if "claude-code" not in available:
        available = ["claude-code", *available]
    return {"available": available}


@app.get("/health")
async def health():
    bridges_health = {}
    if hasattr(app.state, "bridge_manager"):
        for name, bridge in app.state.bridge_manager._bridges.items():
            bridges_health[name] = {"healthy": bridge.healthy}
    return {"status": "ok", "bridges": bridges_health}


# Serve built frontend as static files (SPA catch-all).
# Mounted after API routes so /api/*, /ws, /health take priority.
_dist_dir = Path(__file__).resolve().parent.parent / "web" / "dist"
if _dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist_dir), html=True), name="spa")


def run():
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
