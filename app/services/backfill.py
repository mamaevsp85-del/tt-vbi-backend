from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


async def backfill_dates(
    db: Session,
    sport: str,
    *,
    days: int,
    respect_quota: bool = True,
) -> dict[str, Any]:
    """
    Заглушка для backfill.

    В текущей версии проекта endpoint `/api/v1/backfill` не используется в
    основном цикле (refresh/settle), но импорт модуля нужен, чтобы сервер стартовал.
    """
    return {
        "status": "not_implemented",
        "mode": "backfill_dates",
        "sport": sport,
        "days": days,
        "respect_quota": respect_quota,
    }


async def backfill_players(
    db: Session,
    sport: str,
    *,
    limit: int | None = None,
    respect_quota: bool = True,
) -> dict[str, Any]:
    """Заглушка backfill по игрокам (см. backfill_dates)."""
    return {
        "status": "not_implemented",
        "mode": "backfill_players",
        "sport": sport,
        "limit": limit,
        "respect_quota": respect_quota,
    }

