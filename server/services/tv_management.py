"""Account cancellation surface independent of public TV discovery/playback gates."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

from server.middleware.auth import get_current_user
from server.services.dependencies import get_billing_service
from server.services.tv_billing import Consent, TVError
from server.services.tv_payments import TVPayments

PRIVATE = {
    "Cache-Control": "private, no-store",
    "Vary": "Authorization",
    "X-Robots-Tag": "noindex, nofollow",
}
_active = 0


class ManagementRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handle(request: Request):
            global _active
            if _active >= 8:
                return JSONResponse(
                    {"detail": "TV account management is busy."}, status_code=503, headers=PRIVATE
                )
            _active += 1
            try:
                async with asyncio.timeout(12):
                    body = bytearray()
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > 8192:
                            raise HTTPException(413, "TV account request is too large.")
                        body.extend(chunk)
                    request._body = bytes(body)
                    response = await original(request)
            except (TVError, HTTPException) as exc:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status if isinstance(exc, TVError) else exc.status_code,
                )
            except RequestValidationError:
                response = JSONResponse(
                    {"detail": "Explicit TV account confirmation is required."}, status_code=422
                )
            except Exception:  # noqa: BLE001 - provider exceptions may contain private data
                response = JSONResponse(
                    {"detail": "TV account management is unavailable; retry later."},
                    status_code=503,
                )
            finally:
                _active -= 1
            response.headers.update(PRIVATE)
            return response

        return handle


router = APIRouter(prefix="/billing/tv", tags=["tv-account"], route_class=ManagementRoute)

# Viewing this page performs no account/provider request. The only action opens
# Stripe's explicit cancellation confirmation for the server-owned subscription.
_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Manage TV subscription</title>
<style>:root{color-scheme:light dark}body{max-width:40rem;margin:3rem auto;padding:1rem;
font:1rem/1.6 Georgia,serif}button{font:inherit;padding:.6rem 1rem}</style></head><body>
<h1>Manage your TV subscription</h1>
<p>Cancellation remains available while public TV or new sales are closed.
This action cannot purchase viewing access or add money to your wallet.</p>
<p><a href="/ui/login">Sign in to your existing OpenVegas account</a>, then return here.</p>
<button id="cancel-tv" type="button">Review cancellation in Stripe</button>
<p id="status" role="status">You will confirm cancellation on Stripe. Nothing changes merely by opening this page.</p>
<a href="/ui/balance">Back to wallet</a>
<script type="module">
import {apiFetch,bootstrapBrowserSession} from "/ui/assets/page-auth.js?v=20260330";
const button=document.getElementById("cancel-tv"),status=document.getElementById("status");
button.addEventListener("click",async()=>{
 if(button.disabled)return;button.disabled=true;
 try{
  await bootstrapBrowserSession();
  const response=await apiFetch("/billing/tv/portal",{method:"POST",cache:"no-store",redirect:"error",
   headers:{"Content-Type":"application/json"},body:JSON.stringify({confirmed:true,idempotency_key:crypto.randomUUID()})});
  if(!response.ok)throw new Error();
  const value=await response.json(),url=new URL(value.url);
  if(url.protocol!=="https:"||url.hostname!=="billing.stripe.com"||url.username||url.password||url.port)throw new Error();
  window.location.assign(url.href);
 }catch{status.textContent="Sign in and retry. No TV subscription was changed here; contact support if cancellation remains unavailable.";}
 finally{button.disabled=false;}
});
</script></body></html>"""


@router.get("/manage", response_class=HTMLResponse)
async def management_page():
    return HTMLResponse(_PAGE)


@router.post("/portal")
async def cancellation_portal(payload: Consent, user: Annotated[dict, Depends(get_current_user)]):
    return await TVPayments(get_billing_service()).cancel_portal(user["user_id"], payload)
