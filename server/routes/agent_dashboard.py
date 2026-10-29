"""Organization discovery for the existing agent administration API."""

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from openvegas.enterprise.org_service import OrgService
from server.middleware.auth import get_current_user
from server.services.dependencies import get_db

router = APIRouter(prefix="/v1/agent/organizations", tags=["agent"])
CurrentUser = Annotated[dict, Depends(get_current_user)]


@router.get('/{org_id}/overview')
async def overview(org_id: UUID, response: Response, user: CurrentUser):
    """Read existing ledger balances and open sessions; never fund on a read."""
    response.headers['Cache-Control'] = 'no-store'
    db = get_db()
    async with asyncio.timeout(5):
        membership = await db.fetchrow(
            """SELECT m.role FROM org_members m JOIN organizations o ON o.id=m.org_id
               WHERE m.org_id=$1 AND m.user_id=$2 AND m.status='active'
                 AND m.role IN ('owner', 'admin') AND o.status='active'""",
            str(org_id), user['user_id'],
        )
        if not membership:
            raise HTTPException(403, 'Org owner/admin access required')
        rows = await db.fetch(
            """SELECT a.id, a.name, a.status, w.balance,
                 EXISTS(SELECT 1 FROM agent_sessions s WHERE s.agent_account_id=a.id
                        AND s.org_id=a.org_id AND s.status='active'
                        AND (s.expires_at IS NULL OR s.expires_at>now())) AS inference_open,
                 EXISTS(SELECT 1 FROM casino_sessions c
                        JOIN agent_sessions s ON s.id=c.agent_session_id
                        WHERE c.agent_account_id=a.id AND c.org_id=a.org_id
                          AND c.status='active' AND s.status='active'
                          AND (c.expires_at IS NULL OR c.expires_at>now())
                          AND (s.expires_at IS NULL OR s.expires_at>now())) AS house_open
               FROM agent_accounts a
               LEFT JOIN wallet_accounts w ON w.account_id='agent:' || a.id::text
               WHERE a.org_id=$1 ORDER BY a.created_at DESC, a.id LIMIT 201""",
            str(org_id),
        )
    return {
        'org_id': str(org_id), 'has_more': len(rows) > 200,
        'arena': 'coming_soon',
        'agents': [{
            'id': str(r['id']), 'name': str(r['name']), 'status': str(r['status']),
            'available_balance_v': str(r['balance']) if r['balance'] is not None else None,
            'open_sessions': [mode for mode, key in [('inference', 'inference_open'),
                                                    ('house', 'house_open')] if r[key]],
        } for r in rows[:200]],
    }


@router.get("")
async def organizations(response: Response, user: CurrentUser):
    response.headers["Cache-Control"] = "no-store"
    async with asyncio.timeout(5):
        rows = await get_db().fetch(
            """SELECT o.id, o.name, o.is_demo, m.role FROM organizations o
               JOIN org_members m ON m.org_id = o.id
               WHERE m.user_id = $1 AND m.status = 'active'
                 AND m.role IN ('owner', 'admin') AND o.status = 'active'
               ORDER BY o.created_at DESC LIMIT 100""",
            user["user_id"],
        )
    return {
        "organizations": [{"id": str(r["id"]), "name": r["name"], "role": r["role"],
                           "is_demo": bool(r.get("is_demo", False))} for r in rows]
    }


class CreateOrganization(BaseModel):
    name: str = Field(min_length=1, max_length=80)


@router.post("", status_code=201)
async def create_organization(req: CreateOrganization, response: Response, user: CurrentUser):
    name = req.name.strip()
    if not name or any(ord(c) < 32 for c in name):
        raise HTTPException(422, "Enter a printable organization name")
    response.headers["Cache-Control"] = "no-store"
    async with asyncio.timeout(5):
        async with get_db().transaction() as tx:
            # Serialize the per-owner quota and make repeating a name idempotent.
            await tx.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", "org-create:" + user["user_id"]
            )
            rows = await tx.fetch(
                """SELECT o.id, o.name FROM organizations o JOIN org_members m ON m.org_id=o.id
                   WHERE m.user_id=$1 AND m.role='owner'""",
                user["user_id"],
            )
            for row in rows:
                if row["name"] == name:
                    return {"org_id": str(row["id"]), "name": name}
            if len(rows) >= 3:
                raise HTTPException(
                    409, "Three owned organizations already exist. Reuse an existing one."
                )
            result = await OrgService(tx).create_org(name, user["user_id"])
            # New self-service workspaces do not implicitly enable earning or wagering.
            await tx.execute(
                "UPDATE org_policies SET boost_enabled=FALSE, casino_enabled=FALSE WHERE org_id=$1",
                result["org_id"],
            )
            return result
