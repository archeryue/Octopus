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
from .bridges.manager import BridgeManager
from .config import settings
from .tunnel import CloudflareTunnel
from .database import Database
from .notifiers import notifier_manager
from .agent_manager import AgentManager
from .connector_manager import ConnectorManager
from .routers import agents, attachments, bg_tasks as bg_tasks_router, connectors, credentials, files, notifiers, questions, schedules, sessions, ws
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

    yield

    if tunnel:
        await tunnel.stop()

    # Clean up any in-flight OAuth login subprocesses before we tear down DB.
    from .oauth_login import oauth_login_manager
    await oauth_login_manager.shutdown()
    from .codex_login import codex_login_manager
    await codex_login_manager.shutdown()

    await bg_task_manager.shutdown()
    await schedule_runner.shutdown()
    await bridge_manager.stop_all()
    await bridge_manager.unregister_broadcast()
    await db.close()


app = FastAPI(title="Octopus", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router)
app.include_router(sessions.router)
app.include_router(attachments.router)
app.include_router(files.router)
app.include_router(bg_tasks_router.router)
app.include_router(questions.router)
app.include_router(schedules.router)
app.include_router(credentials.router)
app.include_router(connectors.router)
app.include_router(connectors.agent_router)
app.include_router(notifiers.router)
app.include_router(ws.router)


@app.get("/api/backends")
async def list_backends(_: str = Depends(verify_token)):
    """Which AI backends are usable on this host (codex-backend.md §6.1).
    `claude-code` is always listed; `codex` appears only when its binary
    resolves on PATH."""
    from .backends.subprocess_jsonl import _which_with_fallback

    available = ["claude-code"]
    if _which_with_fallback("codex") is not None:
        available.append("codex")
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
