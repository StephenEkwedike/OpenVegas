"""Realtime voice session + relay routes."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import quote_plus

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


def _extract_realtime_token(token_payload: dict[str, object]) -> str | None:
    payload = dict(token_payload or {})
    client_secret = payload.get("client_secret")
    if isinstance(client_secret, dict):
        value = str(client_secret.get("value") or "").strip()
        if value:
            return value
    if isinstance(client_secret, str) and client_secret.strip():
        return client_secret.strip()
    direct = str(payload.get("token") or payload.get("client_token") or "").strip()
    if direct:
        return direct
    fallback = str(os.getenv("OPENAI_API_KEY", "")).strip()
    return fallback or None


def _decode_event_metadata(raw_text: str) -> tuple[str, int]:
    try:
        payload = json.loads(str(raw_text or ""))
    except Exception:
        return "unknown", 0
    if not isinstance(payload, dict):
        return "unknown", 0
    event_type = str(payload.get("type") or "").strip().lower() or "unknown"
    audio_bytes = 0
    if event_type in {"audio.input.append", "input_audio_buffer.append"}:
        # Support both legacy client payload (`pcm16`) and OpenAI realtime payload (`audio`).
        b64 = str(payload.get("pcm16") or payload.get("audio") or "").strip()
        if b64:
            try:
                audio_bytes = len(base64.b64decode(b64, validate=False))
            except Exception:
                audio_bytes = 0
    return event_type, audio_bytes


def _is_cancel_event(event_type: str) -> bool:
    return str(event_type or "").strip().lower() in {
        "response.cancel",
        "interrupt",
        "cancel",
        "input_audio_buffer.clear",
    }


def _is_cancel_ack_event(event_type: str) -> bool:
    return str(event_type or "").strip().lower() in {
        "response.cancelled",
        "response.done",
    }


@asynccontextmanager
async def _connect_realtime_upstream(session) -> object:
    """Open upstream OpenAI realtime websocket using session token or API key."""
    try:
        import websockets  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency missing at runtime
        raise RuntimeError(f"missing_websockets_dependency:{exc}") from exc

    token = _extract_realtime_token(dict(getattr(session, "token_payload", {}) or {}))
    if not token:
        raise RuntimeError("missing_realtime_token")
    model = str(getattr(session, "model", "") or "gpt-4o-realtime-preview")
    url = f"wss://api.openai.com/v1/realtime?model={quote_plus(model)}"
    headers = {
        "Authorization": f"Bearer {token}",
        "OpenAI-Beta": "realtime=v1",
    }
    open_timeout = float(os.getenv("OPENVEGAS_REALTIME_WS_OPEN_TIMEOUT_SEC", "10"))
    ping_interval = float(os.getenv("OPENVEGAS_REALTIME_WS_PING_INTERVAL_SEC", "20"))
    ping_timeout = float(os.getenv("OPENVEGAS_REALTIME_WS_PING_TIMEOUT_SEC", "20"))
    max_size = int(os.getenv("OPENVEGAS_REALTIME_WS_MAX_BYTES", str(2 * 1024 * 1024)))

    ws = None
    connect_kwargs = dict(
        open_timeout=max(1.0, open_timeout),
        close_timeout=3.0,
        ping_interval=max(5.0, ping_interval),
        ping_timeout=max(5.0, ping_timeout),
        max_size=max(64 * 1024, max_size),
    )
    # websockets API changed from extra_headers -> additional_headers across versions.
    try:
        ws = await websockets.connect(url, additional_headers=headers, **connect_kwargs)
    except TypeError:
        ws = await websockets.connect(url, extra_headers=headers, **connect_kwargs)
    except Exception as exc:
        raise RuntimeError(f"upstream_connect_failed:{exc}") from exc
    try:
        yield ws
    finally:
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()


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
    sequence_no = 1
    cancel_grace_sec = max(0.5, float(os.getenv("OPENVEGAS_REALTIME_CANCEL_GRACE_SEC", "2.0")))
    reconnect_max = max(0, min(5, int(os.getenv("OPENVEGAS_REALTIME_UPSTREAM_RECONNECT_MAX", "2"))))
    max_audio_chunk_bytes = max(1024, int(os.getenv("OPENVEGAS_REALTIME_MAX_CLIENT_AUDIO_CHUNK_BYTES", "262144")))
    reconnect_attempt = 0
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
            reconnect_requested = False
            reconnect_reason = ""
            try:
                async with _connect_realtime_upstream(session) as upstream_ws:
                    await svc.mark_connected(relay_id=relay_id, connected=True)
                    emit_metric("realtime_relay_upstream_connected_total", {"provider": str(session.provider or "unknown")})
                    cancel_forwarded = False
                    cancel_notified = False
                    cancel_forwarded_at = 0.0
                    cancel_acked = False
                    cancel_outcome_emitted = False

                    async def _client_to_upstream() -> None:
                        nonlocal cancel_forwarded, cancel_forwarded_at, sequence_no
                        while True:
                            msg = await websocket.receive()
                            msg_type = str(msg.get("type") or "")
                            if msg_type == "websocket.disconnect":
                                raise WebSocketDisconnect()
                            text = msg.get("text")
                            data = msg.get("bytes")
                            if text is not None:
                                event_type, audio_bytes = _decode_event_metadata(str(text))
                                if event_type in {"audio.input.append", "input_audio_buffer.append"} and audio_bytes > max_audio_chunk_bytes:
                                    await websocket.send_json(
                                        _ws_payload(
                                            "session.error",
                                            sequence_no,
                                            {"detail": f"audio_chunk_too_large:{audio_bytes}>{max_audio_chunk_bytes}"},
                                        )
                                    )
                                    sequence_no += 1
                                    emit_metric("realtime_backpressure_drop_total", {"reason": "audio_chunk_too_large"})
                                    continue
                                await svc.record_event(
                                    relay_id=relay_id,
                                    event_type=f"client.{event_type}",
                                    input_audio_bytes=audio_bytes,
                                )
                                if _is_cancel_event(event_type):
                                    await svc.request_cancel(relay_id=relay_id, user_id=None, reason="client_interrupt")
                                    emit_metric("realtime_interrupt_total", {"source": "client"})
                                    cancel_forwarded = True
                                    cancel_forwarded_at = time.monotonic()
                                await upstream_ws.send(str(text))
                                continue
                            if isinstance(data, (bytes, bytearray)):
                                await svc.record_event(
                                    relay_id=relay_id,
                                    event_type="client.binary",
                                    input_audio_bytes=int(len(data)),
                                )
                                await upstream_ws.send(bytes(data))

                    async def _upstream_to_client() -> None:
                        nonlocal cancel_acked
                        while True:
                            upstream_msg = await upstream_ws.recv()
                            if isinstance(upstream_msg, (bytes, bytearray)):
                                await websocket.send_bytes(bytes(upstream_msg))
                                await svc.record_event(relay_id=relay_id, event_type="upstream.binary")
                                continue
                            text = str(upstream_msg or "")
                            event_type, _ = _decode_event_metadata(text)
                            await svc.record_event(relay_id=relay_id, event_type=f"upstream.{event_type}")
                            if _is_cancel_ack_event(event_type):
                                cancel_acked = True
                            await websocket.send_text(text)

                    upstream_task = asyncio.create_task(_upstream_to_client())
                    client_task = asyncio.create_task(_client_to_upstream())
                    try:
                        while True:
                            done, _pending = await asyncio.wait(
                                {upstream_task, client_task},
                                timeout=0.25,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if done:
                                for task in done:
                                    exc = task.exception()
                                    if exc and isinstance(exc, WebSocketDisconnect):
                                        reconnect_requested = False
                                        break
                                    if exc:
                                        reconnect_requested = True
                                        reconnect_reason = str(exc)[:300]
                                break

                            row = await svc.get_session(relay_id=relay_id, user_id=None)
                            if not row:
                                await websocket.send_json(
                                    _ws_payload("session.error", sequence_no, {"detail": "session_not_found"})
                                )
                                sequence_no += 1
                                reconnect_requested = False
                                break
                            if row.cancel_requested and not cancel_forwarded:
                                cancel_forwarded = True
                                cancel_forwarded_at = time.monotonic()
                                with contextlib.suppress(Exception):
                                    await upstream_ws.send(json.dumps({"type": "response.cancel"}, separators=(",", ":")))
                                emit_metric("realtime_interrupt_total", {"source": "control"})
                            if row.cancel_requested and not cancel_notified:
                                cancel_notified = True
                                await websocket.send_json(
                                    _ws_payload(
                                        "response.cancelled",
                                        sequence_no,
                                        {"reason": row.cancel_reason or "cancelled"},
                                    )
                                )
                                sequence_no += 1
                            if row.cancel_requested and cancel_forwarded and (
                                cancel_acked or (time.monotonic() - cancel_forwarded_at) >= cancel_grace_sec
                            ):
                                if not cancel_outcome_emitted:
                                    emit_metric(
                                        "realtime_cancel_outcome_total",
                                        {"outcome": "acked" if cancel_acked else "timeout"},
                                    )
                                    cancel_outcome_emitted = True
                                reconnect_requested = False
                                break
                    finally:
                        for task in (upstream_task, client_task):
                            task.cancel()
                            with contextlib.suppress(Exception):
                                await task
                        with contextlib.suppress(Exception):
                            await upstream_ws.close()
                        await svc.mark_connected(relay_id=relay_id, connected=False)
                        emit_metric("realtime_relay_upstream_closed_total", {"provider": str(session.provider or "unknown")})
            except RuntimeError as exc:
                reconnect_requested = True
                reconnect_reason = str(exc)[:300]

            row = await svc.get_session(relay_id=relay_id, user_id=None)
            cancel_requested = bool(getattr(row, "cancel_requested", False)) if row else False
            if reconnect_requested and not cancel_requested and reconnect_attempt < reconnect_max:
                reconnect_attempt += 1
                delay = min(2.0, 0.25 * (2 ** (reconnect_attempt - 1)))
                await websocket.send_json(
                    _ws_payload(
                        "session.reconnecting",
                        sequence_no,
                        {
                            "attempt": reconnect_attempt,
                            "max": reconnect_max,
                            "delay_sec": delay,
                            "reason": reconnect_reason,
                        },
                    )
                )
                sequence_no += 1
                emit_metric("realtime_relay_reconnect_total", {"attempt": str(reconnect_attempt)})
                await asyncio.sleep(delay)
                continue
            if reconnect_requested and reconnect_reason:
                await websocket.send_json(_ws_payload("session.error", sequence_no, {"detail": reconnect_reason}))
                sequence_no += 1
            break
    except RuntimeError as exc:
        await websocket.send_json(_ws_payload("session.error", sequence_no, {"detail": str(exc)[:300]}))
        sequence_no += 1
    finally:
        await svc.close(relay_id=relay_id, status="closed")
        try:
            await websocket.close()
        except Exception:
            pass
