"""Loopback-only, explicitly started Arena pilot API; absent from production app."""

import secrets
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .service import Arena, token_valid


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Create(StrictModel):
    seats: StrictInt = Field(2, ge=2, le=10)
    pool: Literal["agent", "human"] = "agent"
    chips: StrictInt = Field(200, ge=2, le=10000)


class Join(StrictModel):
    source_type: Literal["agent", "human"]
    configuration: dict[str, str] = Field(default_factory=dict)


class Action(StrictModel):
    version: StrictInt = Field(ge=0, le=1_000_000)
    action: Literal["fold", "check", "call", "raise"]
    amount: StrictInt | None = Field(None, ge=0, le=1_000_000)
    request_id: str = Field(min_length=8, max_length=80)


class Rights(StrictModel):
    purpose: Literal["internal_evaluation", "training", "evaluation_delivery"] | None = None
    record_id: str | None = Field(None, max_length=80)


class Grant(StrictModel):
    seats: StrictInt = Field(2, ge=2, le=10)
    chips: StrictInt = Field(200, ge=2, le=10000)
    max_matches: StrictInt = Field(10, ge=1, le=100)
    entry_budget: StrictInt = Field(2000, ge=2, le=1_000_000)
    lifetime_seconds: StrictInt = Field(3600, ge=30, le=86400)
    config: dict[str, str] = Field(default_factory=dict)


class Find(StrictModel):
    request_id: str = Field(min_length=8, max_length=80)


class Checkpoint(StrictModel):
    objective_id: str = Field("unspecified", max_length=100)
    task_budget: StrictInt | None = Field(None, ge=0, le=1_000_000)


class Choice(StrictModel):
    checkpoint_id: str = Field(max_length=80)
    choice: Literal["enter", "decline", "continue_work", "stop"]
    context_hash: str = Field(max_length=64)
    game_id: Literal["holdem"] | None = None


class TaskOutcome(StrictModel):
    checkpoint_id: str = Field(max_length=80)
    status: Literal["succeeded", "failed", "abandoned", "unknown"]
    evidence_id: str | None = Field(None, max_length=100)


class AgentAction(Action):
    match_id: str = Field(max_length=80)
    revision: StrictInt = Field(ge=0)


class Intervention(StrictModel):
    command: Literal["pause", "resume", "stop", "override"]
    request_id: str = Field(min_length=8, max_length=80)
    revision: StrictInt = Field(ge=0)
    version: StrictInt | None = Field(None, ge=0)
    action: Literal["fold", "check", "call", "raise"] | None = None
    amount: StrictInt | None = Field(None, ge=0, le=1_000_000)


def make_app(arena: Arena, operator_token: str):
    token_valid(operator_token)
    app = FastAPI(title="OpenVegas Arena / no-stakes local pilot", docs_url=None, redoc_url=None)
    from .autonomy import Autonomy

    autonomy = Autonomy(arena)
    from .checkpoints import Checkpoints
    checkpoints = Checkpoints(arena)

    @app.middleware("http")
    async def local_only(request: Request, next_handler):
        if (
            not request.client
            or request.client.host not in {"127.0.0.1", "::1"}
            or request.headers.get("origin")
        ):
            return JSONResponse({"detail": "loopback_native_clients_only"}, status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 32768:
                return JSONResponse({"detail": "request_too_large"}, status_code=413)
        request._body = bytes(body)
        response = await next_handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def bad_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PermissionError)
    async def forbidden(request, exc):
        return JSONResponse({"detail": "invalid_capability_or_missing_consent"}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Default validation echoes submitted fields, potentially including secrets.
        return JSONResponse({"detail": "invalid_request_fields"}, status_code=422)

    def bearer(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "arena_capability_required")
        return token_valid(authorization[7:])

    @app.get("/health")
    def health():
        return {"mode": "local_no_stakes", "wallet_connected": False, "public_release": False}

    @app.post("/matches")
    def create(req: Create, token=Depends(bearer)):
        if not secrets.compare_digest(token, operator_token):
            raise HTTPException(403, "operator_required")
        return arena.create(req.seats, req.pool, req.chips)

    @app.post("/matches/{match_id}/join")
    def join(match_id: str, req: Join, token=Depends(bearer), x_arena_seat: str = Header()):
        return arena.join(match_id, token, x_arena_seat, req.source_type, req.configuration)

    @app.get("/matches/{match_id}")
    def observe(match_id: str, token=Depends(bearer)):
        return arena.observe(match_id, token)

    @app.post("/matches/{match_id}/actions")
    def action(match_id: str, req: Action, token=Depends(bearer)):
        return arena.act(match_id, token, req.version, req.action, req.request_id, req.amount)

    @app.post("/matches/{match_id}/rights")
    def rights(match_id: str, req: Rights, token=Depends(bearer)):
        return arena.consent(match_id, token, req.purpose, req.record_id)

    @app.post("/grants")
    def grant(req: Grant, token=Depends(bearer)):
        if not secrets.compare_digest(token, operator_token):
            raise HTTPException(403, "operator_required")
        return autonomy.issue(**req.model_dump())

    @app.get("/agent")
    def agent_status(token=Depends(bearer)):
        return autonomy.observe(token)

    @app.get("/agent/games")
    def games(token=Depends(bearer)):
        return checkpoints.games(token)

    @app.post("/agent/checkpoints")
    def checkpoint(req: Checkpoint, token=Depends(bearer)):
        return checkpoints.create(token, **req.model_dump())

    @app.post("/agent/choose")
    def choose(req: Choice, token=Depends(bearer)):
        return checkpoints.choose(token, **req.model_dump())

    @app.post("/agent/task-outcome")
    def outcome(req: TaskOutcome, token=Depends(bearer)):
        return checkpoints.outcome(token, **req.model_dump())

    @app.get("/controller/checkpoints")
    def inspect_checkpoints(token=Depends(bearer)):
        return checkpoints.inspect(token)

    @app.post("/agent/find")
    def find(req: Find, token=Depends(bearer)):
        return autonomy.find(token, req.request_id)

    @app.post("/agent/act")
    def agent_action(req: AgentAction, token=Depends(bearer)):
        return autonomy.act(token, **req.model_dump())

    @app.get("/controller")
    def controller_status(token=Depends(bearer)):
        return autonomy.inspect(token)

    @app.post("/controller")
    def control(req: Intervention, token=Depends(bearer)):
        return autonomy.control(token, **req.model_dump())

    @app.post("/controller/rights/{match_id}")
    def controller_rights(match_id: str, req: Rights, token=Depends(bearer)):
        return autonomy.consent(token, match_id, req.purpose, req.record_id)

    return app
