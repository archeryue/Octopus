import asyncio
import json
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from ..config import settings
from ..session_manager import session_manager

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(...)):
    if token != settings.auth_token:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    logger.info("WebSocket client connected")

    # Register broadcast callback for this connection
    async def broadcast(msg: dict):
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    session_manager.on_broadcast(broadcast)

    # Track background tasks for streaming responses
    tasks: set[asyncio.Task] = set()

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = data.get("type")

            if msg_type == "send_message":
                session_id = data.get("session_id")
                content = data.get("content", "")
                if not session_id or not content:
                    await ws.send_json(
                        {"type": "error", "message": "session_id and content required"}
                    )
                    continue

                # Stream response in background so we can still receive messages
                task = asyncio.create_task(
                    _stream_response(ws, session_id, content)
                )
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            elif msg_type == "approve_tool":
                session_id = data.get("session_id")
                tool_use_id = data.get("tool_use_id")
                if session_id and tool_use_id:
                    ok = await session_manager.approve_tool(session_id, tool_use_id)
                    if not ok:
                        await ws.send_json(
                            {
                                "type": "error",
                                "session_id": session_id,
                                "message": "No pending approval found",
                            }
                        )

            elif msg_type == "deny_tool":
                session_id = data.get("session_id")
                tool_use_id = data.get("tool_use_id")
                reason = data.get("reason", "")
                if session_id and tool_use_id:
                    ok = await session_manager.deny_tool(
                        session_id, tool_use_id, reason
                    )
                    if not ok:
                        await ws.send_json(
                            {
                                "type": "error",
                                "session_id": session_id,
                                "message": "No pending approval found",
                            }
                        )

            else:
                logger.debug("Ignoring unknown client message type: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket error")
    finally:
        session_manager.remove_broadcast(broadcast)
        for t in tasks:
            t.cancel()


async def _stream_response(ws: WebSocket, session_id: str, content: str):
    try:
        async for event in session_manager.send_message(session_id, content):
            try:
                await ws.send_json(event)
            except Exception:
                break
    except Exception as e:
        logger.exception("Stream error for session %s", session_id)
        try:
            await ws.send_json(
                {"type": "error", "session_id": session_id, "message": str(e)}
            )
        except Exception:
            pass
