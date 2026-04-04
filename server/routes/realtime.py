"""Realtime voice session + relay routes."""

from __future__ import annotations

import asyncio
import base64
import json

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from openvegas.capabilities import resolve_capability
from openvegas.flags import features
from openvegas.telemetry import emit_metric
from server.middleware.auth import get_current_user
from server.services.dependencies import get_gateway, get_realtime_relay_service

router = APIRouter()


class RealtimeSessionRequest(BaseModel):
    provider: str = Field(default="openai")
    model: str = Field(default="gpt-4o-realtime-preview")
    voice: str = Field(default="alloy")

    model_config = ConfigDict(extra="forbid")


class RealtimeCancelRequest(BaseModel):
    reason: str = Field(default="user_cancel", max_length=120)

    model_config = ConfigDict(extra="forbid")


def _realtime_enabled() -> bool:
    return bool(features().get("realtime_voice", False))


def _ws_payload(event_type: str, sequence_no: int, payload: dict | None = None) -> dict:
    return {"type": str(event_type or "unknown"), "sequence_no": int(sequence_no), "payload": dict(payload or {})}


@router.post("/realtime/session")
async def create_realtime_session(req: RealtimeSessionRequest, user: dict = Depends(get_current_user)):
    if not _realtime_enabled():
        return JSONResponse(
            status_code=503,
            content={"error": "feature_disabled", "detail": "realtime voice disabled"},
        )
    uid = str(user["user_id"])
    if not resolve_capability(req.provider, req.model, "realtime_voice", user_id=uid):
        return JSONResponse(
            status_code=400,
            content={"error": "capability_unavailable:realtime_voice", "detail": "Realtime voice unavailable"},
        )
    gateway = get_gateway()
    try:
        token_payload = await gateway.create_realtime_session(
            provider=req.provider,
            model=req.model,
            voice=req.voice,
        )
    except Exception as exc:
        return JSONResponse(status_code=502, content={"error": "realtime_session_failed", "detail": str(exc)})

    relay = await get_realtime_relay_service().create_session(
        user_id=uid,
        provider=req.provider,
        model=req.model,
        voice=req.voice,
        token_payload=token_payload if isinstance(token_payload, dict) else {"raw": token_payload},
    )
    emit_metric("realtime_session_created_total", {"provider": req.provider, "model": req.model})
    return {
        **(token_payload if isinstance(token_payload, dict) else {"token_payload": token_payload}),
        "relay_session_id": relay.id,
        "relay_ws_path": f"/realtime/relay/{relay.id}/ws",
    }


@router.post("/realtime/relay/{relay_id}/cancel")
async def cancel_realtime_relay(relay_id: str, req: RealtimeCancelRequest, user: dict = Depends(get_current_user)):
    if not _realtime_enabled():
        return JSONResponse(
            status_code=503,
            content={"error": "feature_disabled", "detail": "realtime voice disabled"},
        )
    uid = str(user["user_id"])
    svc = get_realtime_relay_service()
    ok = await svc.request_cancel(relay_id=relay_id, user_id=uid, reason=req.reason)
    if not ok:
        return JSONResponse(status_code=404, content={"error": "not_found", "detail": "relay session not found"})
    emit_metric("realtime_relay_cancel_total", {"reason": str(req.reason or "user_cancel")[:40]})
    return {"relay_session_id": relay_id, "status": "cancel_requested", "reason": req.reason}


@router.websocket("/realtime/relay/{relay_id}/ws")
async def realtime_relay_ws(relay_id: str, websocket: WebSocket):
    svc = get_realtime_relay_service()
    session = await svc.get_session(relay_id=relay_id, user_id=None)
    if not session:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    await svc.mark_connected(relay_id=relay_id, connected=True)
    sequence_no = 1
    try:
        await websocket.send_json(
            _ws_payload(
                "session.started",
                sequence_no,
                {
                    "relay_session_id": relay_id,
                    "provider": session.provider,
                    "model": session.model,
                    "voice": session.voice,
                },
            )
        )
        sequence_no += 1

        while True:
            row = await svc.get_session(relay_id=relay_id, user_id=None)
            if not row:
                await websocket.send_json(_ws_payload("session.error", sequence_no, {"detail": "session_not_found"}))
                sequence_no += 1
                break
            if row.cancel_requested:
                await websocket.send_json(
                    _ws_payload(
                        "response.cancelled",
                        sequence_no,
                        {"reason": row.cancel_reason or "cancelled"},
                    )
                )
                sequence_no += 1
                break

            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=0.75)
            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break

            try:
                event = json.loads(raw)
            except Exception:
                event = {"type": "invalid_json", "raw": raw}

            evt_type = str((event or {}).get("type") or "").strip().lower()
            if evt_type in {"response.cancel", "interrupt", "cancel"}:
                await svc.request_cancel(relay_id=relay_id, user_id=None, reason="client_interrupt")
                await websocket.send_json(
                    _ws_payload(
                        "response.cancelled",
                        sequence_no,
                        {"reason": "client_interrupt"},
                    )
                )
                sequence_no += 1
                break

            if evt_type == "audio.input.append":
                b64 = str((event or {}).get("pcm16") or "")
                try:
                    chunk_bytes = len(base64.b64decode(b64, validate=False)) if b64 else 0
                except Exception:
                    chunk_bytes = 0
                await svc.record_event(relay_id=relay_id, event_type=evt_type, input_audio_bytes=chunk_bytes)
                await websocket.send_json(
                    _ws_payload(
                        "audio.input.ack",
                        sequence_no,
                        {"bytes": chunk_bytes, "chunk_count": 1},
                    )
                )
                sequence_no += 1
                continue

            if evt_type == "response.create":
                await svc.record_event(relay_id=relay_id, event_type=evt_type)
                await websocket.send_json(_ws_payload("response.started", sequence_no, {}))
                sequence_no += 1
                continue

            if evt_type in {"session.close", "close"}:
                await websocket.send_json(_ws_payload("session.closed", sequence_no, {}))
                sequence_no += 1
                break

            await svc.record_event(relay_id=relay_id, event_type=evt_type or "unknown")
            await websocket.send_json(
                _ws_payload(
                    "relay.unhandled",
                    sequence_no,
                    {"input_type": evt_type or "unknown"},
                )
            )
            sequence_no += 1
    finally:
        await svc.close(relay_id=relay_id, status="closed")
        try:
            await websocket.close()
        except Exception:
            pass

