"""OpenVegas FastAPI backend."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from dotenv import load_dotenv

from server.routes import mint as mint_routes
from server.routes import games as game_routes
from server.routes import wallet as wallet_routes
from server.routes import inference as inference_routes
from server.routes import models as model_routes
from server.routes import casino as casino_routes
from server.routes import store as store_routes
from server.routes import agent as agent_routes
from server.services.dependencies import (
    assert_db_ready,
    assert_redis_ready,
    assert_schema_compatible,
    close_runtime_deps,
    current_flags,
    get_db,
    init_runtime_deps,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
# Allow `uvicorn server.main:app --reload` to work without manually sourcing env vars.
load_dotenv(ROOT_DIR / ".env", override=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_runtime_deps()
    try:
        yield
    finally:
        await close_runtime_deps()

app = FastAPI(
    title="OpenVegas API",
    version="0.1.0",
    description="Terminal Arcade for Developers",
    lifespan=lifespan,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
UI_INDEX = ROOT_DIR / "ui" / "index.html"

app.include_router(mint_routes.router, prefix="/mint", tags=["mint"])
app.include_router(game_routes.router, prefix="/games", tags=["games"])
app.include_router(wallet_routes.router, prefix="/wallet", tags=["wallet"])
app.include_router(inference_routes.router, prefix="/inference", tags=["inference"])
app.include_router(model_routes.router, tags=["models"])
app.include_router(casino_routes.router, tags=["casino"])
app.include_router(store_routes.router, tags=["store"])
app.include_router(agent_routes.router, tags=["agent"])


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/live")
async def health_live():
    return {"status": "up"}


@app.get("/health/ready")
async def health_ready():
    if os.getenv("OPENVEGAS_TEST_MODE", "0") == "1":
        await assert_db_ready()
        await assert_schema_compatible(get_db(), current_flags())
        return {"status": "ready", "mode": "test", "redis": "skipped"}

    await assert_db_ready()
    await assert_redis_ready()
    await assert_schema_compatible(get_db(), current_flags())
    return {"status": "ready", "mode": "runtime"}


@app.get("/ui")
@app.get("/ui/")
async def ui_page():
    return FileResponse(UI_INDEX)
