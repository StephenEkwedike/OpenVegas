"""Avoid reflecting prompts, attachments and rejected credentials in 422 bodies."""
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


class PrivateInferenceRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "Invalid inference request. Check the selected options and input limits."},
                    headers={"Cache-Control": "private, no-store"},
                )

        return private_handler
