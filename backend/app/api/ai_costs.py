"""Estimated AI spend per pipeline step, for administrators."""

import uuid
from typing import Any

from fastapi import APIRouter, Query

from app.api.knowledge_base import SessionDep
from app.security.auth import Admin
from app.services.ai_costs import ai_costs

router = APIRouter(prefix="/api/v1", tags=["ai costs"])


@router.get("/admin/ai-costs")
def get_ai_costs(
    session: SessionDep,
    _: Admin,
    days: int | None = Query(default=None, ge=1, le=3650),
    category_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """What each step cost, estimated at list prices: all time, or the last `days` days, for
    every category or one."""
    return ai_costs(session, days, category_id)
