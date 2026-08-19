from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.core.config import settings
from app.services import research

logger = logging.getLogger(__name__)


def _lines_block(prediction: dict[str, Any]) -> str:
    rows = prediction.get("extra_odds") or []
    if not rows:
        return "Линий доп. рынков в данных нет. Не выдумывай кэфы и числа тотала."
    parts = []
    for row in rows[:24]:
        line = row.get("line")
        parts.append(
            f"- {row.get('market_name') or row.get('market')} "
            f"{row.get('side_name') or row.get('side')} {line}: {row.get('odds')} "
            f"({row.get('bookmaker')})"
        )
    return "Есть линии букмекера:\n" + "\n".join(parts)


async def analyze_match(prediction: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.deepseek_api_key:
        return None

    hints = prediction.get("extra_bets") or []
    hint_txt = json.dumps(hints, ensure_ascii=False) if hints else "нет"

    prompt = f"""
Проанализируй матч по {prediction.get('sport_name')}.
Игрок А: {prediction.get('player_a')} кэф {prediction.get('odds_a')} посев {prediction.get('seed_a')} вероятность модели {prediction.get('prob_a')}
Игрок Б: {prediction.get('player_b')} кэф {prediction.get('odds_b')} посев {prediction.get('seed_b')} вероятность модели {prediction.get('prob_b')}
H2H А:Б = {prediction.get('h2h_home_wins')}:{prediction.get('h2h_away_wins')}
Победитель модели (его и рекомендуй на исход матча): {prediction.get('predicted_winner')}
Кэф ставки модели: {prediction.get('odds_a') if prediction.get('predicted_winner')==prediction.get('player_a') else prediction.get('odds_b')}
Edge: {prediction.get('edge')} вердикт: {prediction.get('verdict')}
Покрытие: {prediction.get('surface') or prediction.get('ground_type')}
Формат best of: {prediction.get('best_of')}
Готовые доп. исходы модели (точный счёт, фора, тотал сетов/партий, первый сет, чёт/нечет): {hint_txt}
{_lines_block(prediction)}

Не путай игроков и кэфы. Рекомендация по исходу матча должна содержать имя победителя модели и его кэф.
Доп. ставки: не подменяй модель, можно коротко подтвердить или предостеречь. Максимум 4 extra_bets.
Если линии нет — не выдумывай кэф и конкретное число тотала геймов.
Доп. рынки это НЕ value, пока нет сверки по сетам.
Ответь только JSON:
{{
  "analysis": "краткий анализ",
  "recommendation": "рекомендация по исходу матча",
  "confidence": "уверенность %",
  "reasoning": "обоснование",
  "value_assessment": "Value Bet да/нет",
  "extra_bets": [
    {{"market": "Точный счёт 2-0", "side": "2-0", "line": "2-0", "odds": null, "why": "почему", "caution": "не value"}}
  ]
}}
"""
    try:
        async with httpx.AsyncClient(timeout=25.0, trust_env=False) as client:
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
                            "content": "Ты эксперт по теннису и настольному теннису. Отвечай только JSON. Не выдумывай кэфы доп. рынков.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 700,
                },
            )
            if response.status_code != 200:
                logger.warning("DeepSeek %s: %s", response.status_code, response.text[:200])
                return None
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                return None
            return json.loads(match.group())
    except Exception:
        logger.exception("DeepSeek не ответил")
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


def needs_research(prediction: dict[str, Any]) -> bool:
    verdict = prediction.get("verdict")
    if verdict == "insufficient_data":
        return True
    if not prediction.get("predicted_winner"):
        return True
    try:
        return abs(float(prediction.get("prob_a") or 0.5) - 0.5) < 0.08
    except (TypeError, ValueError):
        return True


def overlay_forecast(payload: dict[str, Any], analysis: dict[str, Any] | None) -> dict[str, Any]:
    """Наложить прогноз DeepSeek. Value не ставится."""
    if not analysis or not analysis.get("forecast_used"):
        return payload
    side = str(analysis.get("predicted_side") or "").strip().upper()
    player_a = payload.get("player_a") or ""
    player_b = payload.get("player_b") or ""
    winner_name = str(analysis.get("predicted_winner") or "").strip()
    if side == "A":
        winner = player_a
    elif side == "B":
        winner = player_b
    elif winner_name and winner_name.lower() in player_a.lower():
        winner = player_a
        side = "A"
    elif winner_name and winner_name.lower() in player_b.lower():
        winner = player_b
        side = "B"
    else:
        return payload
    existing_source = payload.get("forecast_source")
    existing_risk_tier = payload.get("risk_tier")
    existing_diagnostics = payload.get("model_diagnostics")
    if not isinstance(existing_diagnostics, dict):
        existing_diagnostics = {}
    original_snapshot = {
        "forecast_source": existing_source,
        "risk_tier": existing_risk_tier,
        "predicted_winner": payload.get("predicted_winner"),
        "prob_a": payload.get("prob_a"),
        "prob_b": payload.get("prob_b"),
        "confidence": payload.get("confidence"),
        "verdict": payload.get("verdict"),
    }
    try:
        prob_a = float(analysis.get("prob_a"))
    except (TypeError, ValueError):
        prob_a = 0.62 if side == "A" else 0.38
    prob_a = max(0.18, min(0.82, prob_a))
    if side == "B":
        prob_a = min(prob_a, 0.49)
    else:
        prob_a = max(prob_a, 0.51)
    payload["prob_a"] = round(prob_a, 4)
    payload["prob_b"] = round(1.0 - prob_a, 4)
    payload["predicted_winner"] = winner
    payload["is_value"] = False
    payload["is_signal"] = False
    payload["edge"] = 0.0
    payload["kelly_fraction"] = 0.0
    payload["verdict"] = "ai_research"
    payload["forecast_source"] = "deepseek_overlay"
    payload["risk_tier"] = existing_risk_tier
    try:
        conf = float(str(analysis.get("confidence") or "0").replace("%", "").strip())
        if conf > 1:
            conf = conf / 100.0
    except (TypeError, ValueError):
        conf = 0.5 + abs(prob_a - 0.5) * 0.7
    payload["confidence"] = round(max(0.5, min(0.68, conf)), 3)
    payload["model_diagnostics"] = {
        **existing_diagnostics,
        "overlay_applied": True,
        "overlay_provider": "deepseek",
        "overlay_original": original_snapshot,
    }
    payload["extra_bets"] = extra_bet_hints_safe(payload)
    return payload


def extra_bet_hints_safe(payload: dict[str, Any]) -> list:
    from app.services.markets import extra_bet_hints

    return extra_bet_hints(payload, payload.get("extra_odds") or [])


async def research_forecast(prediction: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.deepseek_api_key:
        return None
    dossier = research.dossier(prediction)
    web = await research.web_brief(prediction)
    prompt = f"""
Модель Elo часто не знает ITF W15 / пары. Твоя задача — выжать максимум из блока «ФАКТЫ ИЗ ИНТЕРНЕТА»:
рейтинги, форма, покрытие (hard/clay), H2H, результаты ITF, как играли пары.
Не пиши шаблон «нет данных о форме/рейтингах/H2H/покрытии/ITF W15», если это уже есть в интернет-фактах.
В unknowns указывай ТОЛЬКО то, чего реально нет ни в модели, ни в интернет-блоке.
Не выдумывай цифры, которых нет в тексте. Если интернет дал W/L на харде — используй.
Если фактов мало — бери сторону рынка, но в analysis перечисли, что удалось найти в сети.

{dossier}

{web}

Игрок A: {prediction.get("player_a")}
Игрок B: {prediction.get("player_b")}

Ответь только JSON:
{{
  "analysis": "что нашли в модели и в интернете",
  "predicted_side": "A или B",
  "predicted_winner": "точное имя A или B из запроса",
  "prob_a": 0.61,
  "confidence": "58%",
  "reasoning": "почему эта сторона, со ссылкой на найденные факты",
  "unknowns": "только то, чего нет даже в интернет-фактах, иначе пустая строка",
  "value_assessment": "нет, это не value",
  "forecast_used": true
}}
prob_a — вероятность победы игрока A, от 0.18 до 0.82.
"""
    try:
        async with httpx.AsyncClient(timeout=40.0, trust_env=False) as client:
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
                            "content": "Ты аналитик тенниса. Используй только присланные факты и осторожные выводы. Отвечай JSON.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.15,
                    "max_tokens": 650,
                },
            )
            if response.status_code != 200:
                logger.warning("DeepSeek research %s: %s", response.status_code, response.text[:200])
                return None
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            data = _parse_json(content)
            if not data:
                return None
            data["forecast_used"] = True
            data["research_dossier"] = dossier[:1500]
            return data
    except Exception:
        logger.exception("DeepSeek research не ответил")
        return None

