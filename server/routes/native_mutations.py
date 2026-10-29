"""Owner-authenticated native file preparation; never remote disk attestation."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from openvegas.contracts.errors import APIErrorCode, ContractError
from openvegas.contracts.native_mutation import ApproveNativeMutation, PrepareNativeMutation
from server.middleware.auth import get_current_user
from server.services.dependencies import get_db


class PrivatePreparationRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def private_handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                # FastAPI's default error body includes rejected input/source bytes.
                return JSONResponse(status_code=422, content={"detail": "Invalid native mutation request."})

        return private_handler


router = APIRouter(prefix="/agent/runs", tags=["native-mutations"], route_class=PrivatePreparationRoute)


def _error(exc: ContractError) -> JSONResponse:
    status = 403 if exc.code == APIErrorCode.APPROVAL_REQUIRED else 409
    return JSONResponse(status_code=status, content={"error": exc.code.value, "detail": exc.detail})


@router.post("/{run_id}/native-mutations/prepare")
async def prepare_native_mutation(run_id: str, request: PrepareNativeMutation,
                                  user: Annotated[dict, Depends(get_current_user)]):
    from openvegas.agent.native_mutation_service import NativeMutationService

    try:
        return await NativeMutationService(get_db()).prepare(
            user_id=user["user_id"], run_id=run_id, **request.model_dump(),
        )
    except ContractError as exc:
        return _error(exc)


@router.post("/{run_id}/native-mutations/{preparation_id}/approve")
async def approve_native_mutation(run_id: str, preparation_id: str, request: ApproveNativeMutation,
                                  user: Annotated[dict, Depends(get_current_user)]):
    from openvegas.agent.native_mutation_service import NativeMutationService

    try:
        return await NativeMutationService(get_db()).approve(
            user_id=user["user_id"], run_id=run_id, preparation_id=preparation_id, **request.model_dump(),
        )
    except ContractError as exc:
        return _error(exc)
