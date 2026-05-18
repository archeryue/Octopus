import json
import logging
import uuid

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

    conn_id = uuid.uuid4().hex

    async def broadcast(msg: dict):
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    session_manager.on_broadcast(conn_id, broadcast)

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
                raw_aids = data.get("attachment_ids") or []
                attachment_ids = (
                    [a for a in raw_aids if isinstance(a, str)]
                    if isinstance(raw_aids, list)
                    else []
                )
                # Allow attachment-only turns (no text), but require *some*
                # signal so we don't kick a backend turn off an empty payload.
                if not session_id or (not content and not attachment_ids):
                    await ws.send_json(
                        {"type": "error", "message": "session_id and content required"}
                    )
                    continue

                try:
                    await session_manager.start_message(
                        session_id, content, attachment_ids=attachment_ids
                    )
                except ValueError as e:
                    await ws.send_json(
                        {"type": "error", "session_id": session_id, "message": str(e)}
                    )

            elif msg_type == "interrupt":
                session_id = data.get("session_id")
                if session_id:
                    await session_manager.interrupt(session_id)

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

            elif msg_type == "answer_question":
                session_id = data.get("session_id")
                question_id = data.get("question_id")
                answers = data.get("answers")
                if session_id and question_id and isinstance(answers, list):
                    ok = await session_manager.answer_question(
                        session_id, question_id, answers
                    )
                    if not ok:
                        await ws.send_json(
                            {
                                "type": "error",
                                "session_id": session_id,
                                "message": "No pending question found",
                            }
                        )

            else:
                logger.debug("Ignoring unknown client message type: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception:
        logger.exception("WebSocket error")
    finally:
        session_manager.remove_broadcast(conn_id)
