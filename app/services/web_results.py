from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.models import Match, Prediction, PredictionHistory
from app.services.api_sport import APISportClient, APISportError
from app.services import parser
from app.services import research

logger = logging.getLogger(__name__)


def _to_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_name(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-zа-я0-9]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _winner_side(prediction: Prediction, winner_name: str) -> str | None:
    winner = _normalize_name(winner_name)
    player_a = _normalize_name(prediction.player_a)
    player_b = _normalize_name(prediction.player_b)
    if not winner or not player_a or not player_b:
        return None
    if winner == player_a or winner in player_a or player_a in winner:
        return "home"
    if winner == player_b or winner in player_b or player_b in winner:
        return "away"
    return None


def _parse_json(content: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _confidence_value(value: Any) -> float:
    try:
        score = float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return 0.0
    if score > 1:
        score = score / 100.0
    return max(0.0, min(1.0, score))


def _enough_evidence(result: dict[str, Any], facts: dict[str, Any], winner_side: str | None) -> bool:
    if not bool(result.get("is_result_found")):
        return False
    if winner_side not in {"home", "away"}:
        return False
    summary = str(result.get("evidence_summary") or "").strip()
    pages = facts.get("pages") or []
    confidence_threshold = 0.64

    # Some sources (e.g. Flashscore KZ) can be more/less conservative with confidence,
    # so we lower the bar when they are present.
    if any(
        (
            (page if isinstance(page, str) else page.get("url")) or ""
        ).find("flashscorekz.com") != -1
        for page in pages
    ):
        confidence_threshold = 0.55

    if _confidence_value(result.get("confidence")) < confidence_threshold:
        return False

    return len(summary) >= 20 and len(pages) >= 1


async def _ask_deepseek_for_result(prediction: Prediction, facts: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.deepseek_api_key:
        return None

    pages = facts.get("pages") or []
    if pages:
        fact_lines = []
        for idx, item in enumerate(pages[:3], start=1):
            fact_lines.append(f"{idx}. title: {item.get('title')}")
            fact_lines.append(f"   url: {item.get('url')}")
            if item.get("snippet"):
                fact_lines.append(f"   snippet: {item.get('snippet')}")
            if item.get("excerpt"):
                fact_lines.append(f"   excerpt: {item.get('excerpt')}")
        facts_block = "\n".join(fact_lines)
    else:
        facts_block = "Надежные веб-страницы не найдены."

    prompt = f"""
Найди итог матча по интернет-фактам. Не выдумывай и не угадывай.
Матч:
- sport: {prediction.sport}
- tournament: {prediction.tournament}
- scheduled_at: {prediction.scheduled_at}
- player_a: {prediction.player_a}
- player_b: {prediction.player_b}

ФАКТЫ ИЗ ИНТЕРНЕТА:
{facts_block}

Правила:
- Если результата недостаточно, верни is_result_found=false.
- winner_name должен быть ровно одним из игроков матча или пустой строкой.
- result_score можно оставить пустым.
- evidence_summary коротко опиши, какие источники и факты подтверждают вывод.
- confidence укажи числом 0..1 или строкой процента.
- Ответь только JSON.

{{
  "is_result_found": true,
  "winner_name": "",
  "result_score": "",
  "evidence_summary": "Короткая выжимка по источникам и итоговому счету.",
  "confidence": 0.78
}}
"""
    try:
        async with httpx.AsyncClient(timeout=35.0, trust_env=True) as client:
            response = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Ты валидатор результатов тенниса и настольного тенниса. "
                                "Используй только присланные веб-факты. Отвечай только JSON."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 400,
                },
            )
            if response.status_code != 200:
                logger.warning("DeepSeek result settlement %s: %s", response.status_code, response.text[:200])
                return None
    except Exception:
        logger.exception("DeepSeek result settlement request failed")
        return None

    content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    return _parse_json(content)


def _apply_settlement(db: Session, prediction: Prediction, winner_side: str, result_data: dict[str, Any]) -> PredictionHistory:
    actual_winner = prediction.player_a if winner_side == "home" else prediction.player_b
    pick_is_home = prediction.predicted_winner == prediction.player_a
    won = (winner_side == "home" and pick_is_home) or (winner_side == "away" and not pick_is_home)
    stake_odds = prediction.odds_a if pick_is_home else prediction.odds_b
    if won and stake_odds:
        pnl = round(stake_odds - 1.0, 4)
    elif won:
        pnl = 1.0
    else:
        pnl = -1.0

    history = PredictionHistory(
        prediction_id=prediction.id,
        external_id=prediction.external_id,
        result="win" if won else "loss",
        actual_winner=actual_winner,
        profit_loss=pnl,
        roi=pnl,
    )
    db.add(history)
    prediction.settled = True

    match_row = db.query(Match).filter(Match.external_id == prediction.external_id).one_or_none()
    if match_row is not None:
        match_row.status = "finished"
        match_row.winner = winner_side
        score = str(result_data.get("result_score") or "").strip()
        if score:
            match_row.score_display = score
    return history


async def _settle_with_api_sport(
    db: Session,
    prediction: Prediction,
    *,
    days_window: int = 2,
    page_size: int = 50,
    pages_to_scan: int = 3,
) -> dict[str, Any] | None:
    """API-Sport fallback when web sources are empty (DDG/DeepSeek can't validate)."""
    scheduled_at = _to_utc(prediction.scheduled_at)
    if scheduled_at is None:
        return None

    client = APISportClient()
    try:
        # API-Sport позволяет получить конкретный матч по ID, что быстрее и дешевле
        # чем сканировать списки finished.
        raw = await client.get_match_by_id(
            prediction.sport,
            prediction.external_id,
            with_pregame=True,
            with_bk_odds=False,
        )
    except APISportError as exc:
        logger.warning("API-Sport fallback failed for %s: %s", prediction.external_id, exc)
        return None
    except Exception:
        logger.exception("API-Sport fallback unexpected error for %s", prediction.external_id)
        return None

    if not raw:
        return None

    core = parser.parse_core(raw, prediction.sport)
    status = (core.get("status") or "").strip().lower()
    winner_side = str(core.get("winner") or "").strip()
    if status != "finished":
        return None
    if winner_side not in {"home", "away"}:
        return None

    result_score = str(core.get("score_display") or "").strip()
    _apply_settlement(db, prediction, winner_side, {"result_score": result_score})

    winner_name = prediction.player_a if winner_side == "home" else prediction.player_b
    return {
        "winner_name": winner_name,
        "winner_side": winner_side,
        "result_score": result_score,
        "confidence": prediction.confidence,
        "evidence_summary": "API-Sport match-by-id fallback (web sources not found).",
    }


async def settle_unsettled_predictions_web(
    db: Session,
    *,
    limit: int | None = None,
    grace_minutes: int = 30,
) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(1, grace_minutes))
    rows = (
        db.query(Prediction)
        .filter(Prediction.settled.is_(False), Prediction.scheduled_at.is_not(None))
        .order_by(Prediction.scheduled_at.asc(), Prediction.id.asc())
        .all()
    )

    checked: list[dict[str, Any]] = []
    settled: list[dict[str, Any]] = []
    skipped = 0

    for prediction in rows:
        scheduled_at = _to_utc(prediction.scheduled_at)
        if scheduled_at is None or scheduled_at > cutoff:
            continue
        if limit is not None and len(checked) >= max(1, limit):
            break

        existing = (
            db.query(PredictionHistory)
            .filter(PredictionHistory.prediction_id == prediction.id)
            .order_by(PredictionHistory.id.desc())
            .first()
        )
        if existing is not None:
            prediction.settled = True
            skipped += 1
            checked.append(
                {
                    "prediction_id": prediction.id,
                    "external_id": prediction.external_id,
                    "players": f"{prediction.player_a} vs {prediction.player_b}",
                    "scheduled_at": scheduled_at.isoformat(),
                    "status": "already_in_history",
                }
            )
            continue

        payload = {
            "sport": prediction.sport,
            "sport_name": prediction.sport_name,
            "tournament": prediction.tournament,
            "scheduled_at": prediction.scheduled_at,
            "player_a": prediction.player_a,
            "player_b": prediction.player_b,
        }
        facts: dict[str, Any] = {"queries": [], "results": [], "pages": []}
        result_data: dict[str, Any] | None = None
        status = "no_result"

        # Для настольного тенниса текущий DDG-путь часто виснет на таймаутах,
        # поэтому сначала делаем быстрый API-Sport fallback.
        if prediction.sport == "table_tennis":
            api_settlement = await _settle_with_api_sport(db, prediction)
            if api_settlement is not None:
                winner_name = api_settlement["winner_name"]
                winner_side = api_settlement["winner_side"]
                status = "settled"
                settled.append(
                    {
                        "prediction_id": prediction.id,
                        "external_id": prediction.external_id,
                        "players": f"{prediction.player_a} vs {prediction.player_b}",
                        "winner_name": winner_name,
                        "result_score": api_settlement.get("result_score"),
                        "confidence": _confidence_value(api_settlement.get("confidence")),
                        "evidence_summary": api_settlement.get("evidence_summary"),
                    }
                )
                result_data = {
                    "is_result_found": True,
                    "winner_name": winner_name,
                    "result_score": api_settlement.get("result_score", ""),
                    "evidence_summary": api_settlement.get("evidence_summary", ""),
                    "confidence": api_settlement.get("confidence", 0.0),
                }
                checked.append(
                    {
                        "prediction_id": prediction.id,
                        "external_id": prediction.external_id,
                        "players": f"{prediction.player_a} vs {prediction.player_b}",
                        "scheduled_at": scheduled_at.isoformat(),
                        "status": status,
                        "queries": [],
                        "sources": [],
                        "llm_result": result_data,
                    }
                )
                continue
            else:
                # API-Sport ещё не отдал finished-матч по этому ID.
                # Сейчас не дергаем DDG/DeepSeek (там нестабильная сеть/прокси).
                facts = {"queries": [], "results": [], "pages": []}
                result_data = None
        else:
            # Аналогично: сначала пробуем API-Sport.
            # Если не нашли winner — оставляем no_result без веб-валидации.
            facts = {"queries": [], "results": [], "pages": []}
            result_data = None
        winner_name = str((result_data or {}).get("winner_name") or "").strip()
        winner_side = _winner_side(prediction, winner_name)
        if result_data and _enough_evidence(result_data, facts, winner_side):
            _apply_settlement(db, prediction, winner_side, result_data)
            status = "settled"
            settled.append(
                {
                    "prediction_id": prediction.id,
                    "external_id": prediction.external_id,
                    "players": f"{prediction.player_a} vs {prediction.player_b}",
                    "winner_name": winner_name,
                    "result_score": result_data.get("result_score"),
                    "confidence": _confidence_value(result_data.get("confidence")),
                    "evidence_summary": result_data.get("evidence_summary"),
                }
            )
        elif result_data and bool(result_data.get("is_result_found")) and winner_side is None:
            status = "winner_mismatch"
        else:
            # Здесь либо нет result_data, либо winner не распознан.
            # API-Sport уже пытались — веб-валидацию пока не выполняем.
            pass

        checked.append(
            {
                "prediction_id": prediction.id,
                "external_id": prediction.external_id,
                "players": f"{prediction.player_a} vs {prediction.player_b}",
                "scheduled_at": scheduled_at.isoformat(),
                "status": status,
                "queries": facts.get("queries") or [],
                "sources": [page.get("url") for page in (facts.get("pages") or []) if page.get("url")],
                "llm_result": result_data,
            }
        )

    db.commit()
    return {
        "status": "success",
        "checked": len(checked),
        "settled": len(settled),
        "skipped": skipped,
        "grace_minutes": grace_minutes,
        "checked_items": checked,
        "settled_items": settled,
    }
