import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

# Clear this so claude-code-sdk subprocess doesn't think it's nested
os.environ.pop("CLAUDECODE", None)

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .bridges.manager import BridgeManager
from .config import settings
from .tunnel import CloudflareTunnel
from .database import Database
from .routers import sessions, ws
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

app.include_router(sessions.router)
app.include_router(ws.router)


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
