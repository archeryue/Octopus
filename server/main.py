import logging
import os
from contextlib import asynccontextmanager

# Clear this so claude-code-sdk subprocess doesn't think it's nested
os.environ.pop("CLAUDECODE", None)

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import Database
from .routers import sessions, ws
from .session_manager import session_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.db_path)
    await db.initialize()
    await session_manager.initialize(db)
    yield
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
    return {"status": "ok"}


def run():
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )


if __name__ == "__main__":
    run()
