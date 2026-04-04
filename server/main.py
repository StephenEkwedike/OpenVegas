"""OpenVegas FastAPI backend."""

from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from dotenv import load_dotenv
from openvegas.compact_uuid import decode_compact_uuid

from server.routes import mint as mint_routes
from server.routes import games as game_routes
from server.routes import wallet as wallet_routes
from server.routes import inference as inference_routes
from server.routes import models as model_routes
from server.routes import ui_auth as ui_auth_routes
from server.routes import casino as casino_routes
from server.routes import casino_human as casino_human_routes
from server.routes import store as store_routes
from server.routes import agent as agent_routes
from server.routes import agent_orchestration as agent_orchestration_routes
from server.routes import ide_bridge as ide_bridge_routes
from server.routes import payments as payment_routes
from server.routes import profile_preferences as profile_preferences_routes
from server.routes import files as file_upload_routes
from server.routes import mcp as mcp_routes
from server.routes import code_exec as code_exec_routes
from server.routes import image_gen as image_gen_routes
from server.routes import realtime as realtime_routes
from server.routes import ops_diagnostics as ops_diagnostics_routes
from server.services.dependencies import (
    assert_db_ready,
    assert_redis_ready,
    assert_schema_compatible,
    close_runtime_deps,
    current_flags,
    get_db,
    init_runtime_deps,
)
from openvegas.qr_runtime import ensure_qrcode_available

logger = logging.getLogger(__name__)

def _resolve_root_dir() -> Path:
    env_root = os.getenv("OPENVEGAS_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.append(Path.cwd())
    candidates.append(Path(__file__).resolve().parents[1])
    for candidate in candidates:
        try:
            root = candidate.resolve()
        except Exception:
            continue
        if (root / "ui" / "index.html").exists() and (root / "server" / "main.py").exists():
            return root
    return Path(__file__).resolve().parents[1]


ROOT_DIR = _resolve_root_dir()
# Allow `uvicorn server.main:app --reload` to work without manually sourcing env vars.
load_dotenv(ROOT_DIR / ".env", override=False)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_runtime_deps()
    qr_ok, qr_reason = ensure_qrcode_available()
    if not qr_ok:
        logger.warning("QR runtime unavailable at startup: %s", qr_reason)
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

UI_INDEX = ROOT_DIR / "ui" / "index.html"
TOPUP_UI_PAGE = ROOT_DIR / "ui" / "topup.html"
UI_DIR = ROOT_DIR / "ui"
UI_ASSETS_DIR = UI_DIR / "assets"
UI_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

UI_PAGES = {
    "login": UI_DIR / "login.html",
    "product": UI_DIR / "product.html",
    "how-it-works": UI_DIR / "how-it-works.html",
    "pricing": UI_DIR / "pricing.html",
    "balance": UI_DIR / "balance.html",
    "payments": UI_DIR / "payments.html",
    "topup-checkout": UI_DIR / "topup-checkout.html",
    "checkout-pending": UI_DIR / "checkout-pending.html",
    "subscription": UI_DIR / "subscription.html",
    "profile": UI_DIR / "profile.html",
    "transactions": UI_DIR / "transactions.html",
    "checkout-success": UI_DIR / "checkout-success.html",
    "checkout-cancel": UI_DIR / "checkout-cancel.html",
    "security": UI_DIR / "security.html",
    "faq": UI_DIR / "faq.html",
    "how-to-play": UI_DIR / "how-to-play.html",
    "contact": UI_DIR / "contact.html",
}

LEGACY_UI_REDIRECTS = {
    "docs": "/ui/how-to-play",
}

UI_ASSETS = {
    "theme.css": UI_ASSETS_DIR / "theme.css",
    "layout.css": UI_ASSETS_DIR / "layout.css",
    "site.js": UI_ASSETS_DIR / "site.js",
    "page-auth.js": UI_ASSETS_DIR / "page-auth.js",
    "topup.js": UI_ASSETS_DIR / "topup.js",
    "deck.css": UI_ASSETS_DIR / "deck.css",
    "deck.js": UI_ASSETS_DIR / "deck.js",
    "content-registry.js": UI_ASSETS_DIR / "content-registry.js",
    "market-data.json": UI_ASSETS_DIR / "market-data.json",
    "avatar-engine.js": UI_ASSETS_DIR / "avatar-engine.js",
    "avatar-renderer.js": UI_ASSETS_DIR / "avatar-renderer.js",
    "avatar-sprite-loader.js": UI_ASSETS_DIR / "avatar-sprite-loader.js",
    "avatar-widget.js": UI_ASSETS_DIR / "avatar-widget.js",
    "avatar-manifest.json": UI_ASSETS_DIR / "avatar-manifest.json",
    "theme-fallback.js": UI_ASSETS_DIR / "theme-fallback.js",
}

SLIDES = {
    "01-cover.html": UI_DIR / "slidedeck" / "01-cover.html",
    "02-vibecoding-hook.html": UI_DIR / "slidedeck" / "02-vibecoding-hook.html",
    "03-problem.html": UI_DIR / "slidedeck" / "03-problem.html",
    "04-bad-alternatives.html": UI_DIR / "slidedeck" / "04-bad-alternatives.html",
    "05-solution.html": UI_DIR / "slidedeck" / "05-solution.html",
    "06-product.html": UI_DIR / "slidedeck" / "06-product.html",
    "07-validation.html": UI_DIR / "slidedeck" / "07-validation.html",
    "08-market-size.html": UI_DIR / "slidedeck" / "08-market-size.html",
    "09-business-model.html": UI_DIR / "slidedeck" / "09-business-model.html",
    "10-moat.html": UI_DIR / "slidedeck" / "10-moat.html",
    "11-why-now.html": UI_DIR / "slidedeck" / "11-why-now.html",
    "12-ask.html": UI_DIR / "slidedeck" / "12-ask.html",
}

app.include_router(mint_routes.router, prefix="/mint", tags=["mint"])
app.include_router(game_routes.router, prefix="/games", tags=["games"])
app.include_router(wallet_routes.router, prefix="/wallet", tags=["wallet"])
app.include_router(inference_routes.router, prefix="/inference", tags=["inference"])
app.include_router(ui_auth_routes.router, tags=["ui-auth"])
app.include_router(model_routes.router, tags=["models"])
app.include_router(casino_routes.router, tags=["casino"])
app.include_router(casino_human_routes.router, tags=["casino-human"])
app.include_router(store_routes.router, tags=["store"])
app.include_router(agent_routes.router, tags=["agent"])
app.include_router(agent_orchestration_routes.router, tags=["agent-orchestration"])
app.include_router(ide_bridge_routes.router, tags=["ide-bridge"])
app.include_router(payment_routes.router, tags=["billing"])
app.include_router(profile_preferences_routes.router, tags=["profile"])
app.include_router(file_upload_routes.router, tags=["files"])
app.include_router(mcp_routes.router, tags=["mcp"])
app.include_router(code_exec_routes.router, tags=["code-exec"])
app.include_router(image_gen_routes.router, tags=["image-gen"])
app.include_router(realtime_routes.router, tags=["realtime"])
app.include_router(ops_diagnostics_routes.router, tags=["ops"])


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/live")
async def health_live():
    return {"status": "up"}


@app.get("/health/ready")
async def health_ready():
    flags = current_flags()
    human_casino_enabled = bool(flags.human_casino_enabled)
    human_casino_schema_ready = bool(flags.human_casino_enabled)

    if os.getenv("OPENVEGAS_TEST_MODE", "0") == "1":
        await assert_db_ready()
        await assert_schema_compatible(get_db(), flags)
        return {
            "status": "ready",
            "mode": "test",
            "redis": "skipped",
            "human_casino_enabled": human_casino_enabled,
            "human_casino_schema_ready": human_casino_schema_ready,
        }

    await assert_db_ready()
    await assert_redis_ready()
    await assert_schema_compatible(get_db(), flags)
    return {
        "status": "ready",
        "mode": "runtime",
        "human_casino_enabled": human_casino_enabled,
        "human_casino_schema_ready": human_casino_schema_ready,
    }


@app.get("/ui")
@app.get("/ui/")
async def ui_page():
    return FileResponse(UI_INDEX, headers=UI_NO_CACHE_HEADERS)


def _resolve_ui_asset(asset_path: str) -> Path | None:
    raw = str(asset_path or "").strip().lstrip("/")
    if not raw:
        return None

    direct = UI_ASSETS.get(raw)
    if direct and direct.exists() and direct.is_file():
        return direct

    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts:
        return None

    root = UI_ASSETS_DIR.resolve()
    candidate = (root / rel).resolve()
    if not candidate.exists() or not candidate.is_file():
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


@app.get("/ui/assets/{asset_path:path}")
async def ui_asset(asset_path: str):
    asset = _resolve_ui_asset(asset_path)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(asset, headers=UI_NO_CACHE_HEADERS)


@app.get("/ui/slidedeck/{slide_name}")
async def ui_slide_page(slide_name: str):
    page = SLIDES.get(slide_name)
    if not page or not page.exists():
        raise HTTPException(status_code=404, detail="Slide not found")
    return FileResponse(page, headers=UI_NO_CACHE_HEADERS)


@app.get("/ui/topup/{topup_id}")
async def ui_topup_page(topup_id: str):
    if not TOPUP_UI_PAGE.exists():
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(TOPUP_UI_PAGE, headers=UI_NO_CACHE_HEADERS)


@app.get("/t/{topup_id}")
async def ui_topup_short_redirect(topup_id: str):
    return RedirectResponse(url=f"/ui/topup/{topup_id}", status_code=307)


@app.get("/r/{compact_topup_id}")
async def ui_topup_compact_redirect(compact_topup_id: str):
    topup_id = decode_compact_uuid(compact_topup_id)
    if not topup_id:
        raise HTTPException(status_code=404, detail="Top-up not found")
    return RedirectResponse(url=f"/ui/topup/{topup_id}", status_code=307)


@app.get("/ui/{slug}")
async def ui_page_slug(slug: str):
    redirect_to = LEGACY_UI_REDIRECTS.get(slug)
    if redirect_to:
        return RedirectResponse(url=redirect_to, status_code=307)
    page = UI_PAGES.get(slug)
    if not page or not page.exists():
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(page, headers=UI_NO_CACHE_HEADERS)


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def ui_favicon_fallback():
    # Keep logs clean when browsers probe standard icon paths.
    return Response(status_code=204)
