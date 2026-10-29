"""Separate consent for cross-visit measurement. No arbitrary event properties."""
import asyncio
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, StrictBool

from server.middleware.auth import get_current_user
from server.routes.analytics import _activity_environment, _admit_activity
from server.services import product_journeys

router = APIRouter(prefix='/analytics/journey', tags=['analytics'])
_export_slots = asyncio.Semaphore(2)


class Journey(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    browser_id: str
    consent: StrictBool


@router.get('/config')
async def config(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'enabled': product_journeys.enabled()}


async def parse(request):
    if not product_journeys.enabled():
        raise HTTPException(404, 'Journey measurement disabled')
    if request.headers.get('origin') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Same-origin request required')
    if request.headers.get('content-type', '').split(';')[0] != 'application/json':
        raise HTTPException(415, 'JSON required')
    body = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 512:
                    raise HTTPException(413, 'Request too large')
    except TimeoutError:
        raise HTTPException(408, 'Request timed out') from None
    try:
        value = Journey.model_validate_json(bytes(body))
        browser = UUID(value.browser_id)
        if browser.version != 4 or str(browser) != value.browser_id or not value.consent:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, 'Invalid consented journey') from None
    if not _admit_activity(str(browser)):
        raise HTTPException(429, 'Try later')
    environment = _activity_environment(request)
    if environment == 'unclassified':
        raise HTTPException(503, 'Measurement environment not configured')
    return browser, environment


async def export():
    if _export_slots.locked():
        return
    try:
        async with _export_slots:
            async with asyncio.timeout(18):
                await product_journeys.export_pending()
    except Exception:
        pass  # Durable outbox remains retryable; never expose errors or hold up login.


async def capture(request, background, *, registration=False):
    browser, environment = await parse(request)
    user = await get_current_user(request) if registration else None
    try:
        async with asyncio.timeout(3):
            accepted = await product_journeys.record(browser, environment, UUID(user['user_id']) if user else None)
    except Exception:
        return Response(status_code=503, headers={'Cache-Control': 'no-store'})
    if not accepted:
        return Response(status_code=409, headers={'Cache-Control': 'no-store'})
    background.add_task(export)
    return Response(status_code=202, headers={'Cache-Control': 'no-store'})


@router.post('/visit')
async def visit(request: Request, background: BackgroundTasks):
    return await capture(request, background)


@router.post('/registration')
async def registration(request: Request, background: BackgroundTasks):
    return await capture(request, background, registration=True)
