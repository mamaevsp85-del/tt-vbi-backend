from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.models import Match, Prediction, PredictionHistory
from app.services import quota
from app.services.backfill import backfill_dates, backfill_players
from app.services.pipeline import enrich_thin_predictions, prediction_to_dict, refresh_sport
from app.services.quota import QuotaError

KR_TZ = timezone(timedelta(hours=7))


def _prediction_sort_key(item: dict):
    if item.get("is_value"):
        group = 0
    elif item.get("verdict") == "no_value":
        group = 1
    else:
        group = 2
    return (group, -(item.get("kelly_fraction") or 0), -(abs((item.get("prob_a") or 0.5) - 0.5)), item.get("scheduled_at") or "")


def _parse_day(value: str):
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Дата в формате YYYY-MM-DD") from exc


def _kr_date(value: datetime | None):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(KR_TZ).date()

router = APIRouter()


class SettingsUpdate(BaseModel):
    min_odds: Optional[float] = None
    min_edge: Optional[float] = None
    max_signals: Optional[int] = None


@router.post("/refresh")
async def refresh(sport: str = Query("table_tennis"), db: Session = Depends(get_db)):
    if sport not in {"table_tennis", "tennis"}:
        raise HTTPException(status_code=400, detail="sport должен быть table_tennis или tennis")
    try:
        quota.check_refresh(sport)
        result = await refresh_sport(db, sport)
        quota.mark_refresh(sport)
        result["quota"] = quota.snapshot()
        return result
    except QuotaError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        text = str(exc)
        if "429" in text or "tariff_rate_limit" in text:
            quota.mark_exhausted()
            raise HTTPException(
                status_code=429,
                detail="Лимит API-Sport на сегодня исчерпан. Если есть запасной ключ — впишите API_SPORT_KEY_BACKUP в .env и перезапустите бэкенд.",
            ) from exc
        raise HTTPException(status_code=502, detail=text) from exc


@router.get("/quota")
def api_quota():
    return {"status": "success", "data": quota.snapshot()}


@router.post("/backfill")
async def api_backfill(
    sport: str = Query("tennis"),
    days: int | None = Query(None, ge=1, le=365),
    players: bool = Query(False),
    limit: int | None = Query(None, ge=1, le=50),
    ignore_quota: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Разовый бэкфилл истории (не тратит cooldown refresh)."""
    if sport not in {"table_tennis", "tennis"}:
        raise HTTPException(status_code=400, detail="sport: table_tennis или tennis")
    try:
        respect = not ignore_quota
        if players:
            result = await backfill_players(db, sport, limit=limit, respect_quota=respect)
        else:
            result = await backfill_dates(db, sport, days=days, respect_quota=respect)
        return result
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/enrich-ai")
async def enrich_ai(
    sport: str = Query("tennis"),
    limit: int | None = Query(None, ge=1, le=20),
    force: bool = Query(False),
    db: Session = Depends(get_db),
):
    """Исследование слабых матчей через DeepSeek. Квоту API-Sport не тратит."""
    if sport not in {"table_tennis", "tennis"}:
        raise HTTPException(status_code=400, detail="sport: table_tennis или tennis")
    try:
        return await enrich_thin_predictions(db, sport, limit=limit, force=force)
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/predictions/today")
def today_predictions(
    sport: Optional[str] = Query("table_tennis"),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    min_odds: Optional[float] = Query(None, ge=1),
    max_odds: Optional[float] = Query(None, ge=1),
    show_value_only: bool = Query(False),
    min_confidence: Optional[float] = Query(None, ge=0, le=1),
    within_hours: Optional[int] = Query(None, ge=1, le=24),
    db: Session = Depends(get_db),
):
    query = db.query(Prediction).filter(Prediction.settled.is_(False))
    if sport:
        query = query.filter(Prediction.sport == sport)
    rows = query.order_by(Prediction.scheduled_at.asc()).all()
    ext_ids = [row.external_id for row in rows]
    raw_by_id = {}
    match_by_id = {}
    if ext_ids:
        for match in db.query(Match).filter(Match.external_id.in_(ext_ids)).all():
            raw_by_id[match.external_id] = match.raw
            match_by_id[match.external_id] = match
    items = [
        prediction_to_dict(row, match_raw=raw_by_id.get(row.external_id), match_row=match_by_id.get(row.external_id))
        for row in rows
    ]
    cache_total = len(items)
    notice = None

    start_day = _parse_day(date_from) if date_from else None
    end_day = _parse_day(date_to) if date_to else None
    if start_day and end_day and start_day > end_day:
        start_day, end_day = end_day, start_day

    def item_day(item: dict):
        raw = item.get("scheduled_at")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00") if str(raw).endswith("Z") else str(raw))
        except ValueError:
            return None
        return _kr_date(dt)

    available = sorted({d.isoformat() for d in (item_day(item) for item in items) if d})

    def in_window(item: dict, start, end) -> bool:
        local = item_day(item)
        if local is None:
            return False
        if start and local < start:
            return False
        if end and local > end:
            return False
        return True

    if start_day or end_day:
        if not within_hours:
            dated = [item for item in items if in_window(item, start_day, end_day)]
            if not dated:
                rng = []
                if start_day:
                    rng.append(start_day.strftime("%d.%m.%Y"))
                if end_day and end_day != start_day:
                    rng.append(end_day.strftime("%d.%m.%Y"))
                have = ", ".join(datetime.fromisoformat(d).strftime("%d.%m.%Y") for d in available[:6]) or "нет"
                notice = f"На {('–'.join(rng)) or 'выбранные даты'} матчей нет. В кэше: {have}."
            items = dated

    if min_odds is not None:
        items = [item for item in items if item.get("odds_a") is not None and item["odds_a"] >= min_odds]
    if max_odds is not None:
        items = [item for item in items if item.get("odds_a") is not None and item["odds_a"] <= max_odds]
    if min_confidence is not None:
        items = [item for item in items if (item.get("confidence") or 0) >= min_confidence]
    if show_value_only:
        items = [item for item in items if item.get("is_value")]

    if within_hours:
        now = datetime.now(timezone.utc)
        until = now + timedelta(hours=within_hours)
        past = now - timedelta(minutes=15)

        def item_start(item: dict):
            raw = item.get("scheduled_at")
            if not raw:
                return None
            text = str(raw)
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        pool = items
        timed = [item for item in items if (start := item_start(item)) is not None and past <= start <= until]
        if not timed:
            future = []
            for item in pool:
                start = item_start(item)
                if start is not None and start > now:
                    future.append(start)
            if future:
                nxt = min(future)
                hours_away = (nxt - now).total_seconds() / 3600
                kr = nxt.astimezone(KR_TZ).strftime("%H:%M")
                suggest = 8 if hours_away <= 8 else 12 if hours_away <= 12 else 24
                notice = (
                    f"В ближайшие {within_hours} ч матчей нет. "
                    f"Ближайший в {kr} по Красноярску (через {hours_away:.1f} ч). "
                    f"Поставьте фильтр {suggest} ч."
                )
            else:
                notice = f"В ближайшие {within_hours} ч матчей нет." + (
                    f" В кэше даты: {', '.join(datetime.fromisoformat(d).strftime('%d.%m.%Y') for d in available[:6])}."
                    if available else ""
                )
        items = timed

    items.sort(key=_prediction_sort_key)
    return {
        "status": "success",
        "data": items,
        "total": len(items),
        "sport_filter": sport,
        "source": "API-Sport",
        "notice": notice,
        "available_dates": available if start_day or end_day else [],
        "cache_total": cache_total,
    }


@router.get("/predictions/history")
def prediction_history(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(PredictionHistory)
        .order_by(PredictionHistory.created_at.desc())
        .limit(200)
        .all()
    )
    return {
        "status": "success",
        "data": [
            {
                "prediction_id": row.prediction_id,
                "match_id": row.external_id,
                "result": row.result,
                "actual_winner": row.actual_winner,
                "profit_loss": row.profit_loss,
                "roi": row.roi,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
        "meta": {"days": days, "total": len(rows)},
    }


@router.get("/stats/model")
def model_stats(db: Session = Depends(get_db)):
    total = db.query(func.count(PredictionHistory.id)).scalar() or 0
    wins = db.query(func.count(PredictionHistory.id)).filter(PredictionHistory.result == "win").scalar() or 0
    pnl = db.query(func.coalesce(func.sum(PredictionHistory.profit_loss), 0.0)).scalar() or 0
    value_found = db.query(func.count(Prediction.id)).filter(Prediction.is_value.is_(True)).scalar() or 0
    accuracy = round(wins / total, 3) if total else 0
    roi = round(pnl / total, 3) if total else 0
    return {
        "status": "success",
        "data": {
            "accuracy": accuracy,
            "roi": roi,
            "total_predictions": total,
            "value_bets_found": value_found,
        },
    }


@router.get("/settings")
def get_settings():
    return {
        "status": "success",
        "data": {
            "min_odds": settings.min_odds_threshold,
            "min_edge": settings.min_edge_threshold,
            "max_signals": settings.max_daily_signals,
            "llm_model": "deepseek-chat",
        },
    }


@router.put("/settings")
def update_settings(body: SettingsUpdate):
    if body.min_odds is not None:
        settings.min_odds_threshold = body.min_odds
    if body.min_edge is not None:
        settings.min_edge_threshold = body.min_edge
    if body.max_signals is not None:
        settings.max_daily_signals = body.max_signals
    return {
        "status": "success",
        "message": "Настройки обновлены (до перезапуска процесса)",
        "data": {
            "min_odds": settings.min_odds_threshold,
            "min_edge": settings.min_edge_threshold,
            "max_signals": settings.max_daily_signals,
        },
    }


@router.get("/matches/live")
def live_matches():
    return {
        "status": "success",
        "data": [],
        "message": "Live — следующий шаг. Сейчас приоритет у прематч value.",
    }
