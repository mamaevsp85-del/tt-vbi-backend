from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.models import Match, MatchStat, Prediction, PredictionHistory, utcnow
from app.services import deepseek, elo, parser
from app.services.api_sport import APISportClient
from app.services.predictor import build_prediction, extra_bet_hints, merge_extra_bets
from app.services import quota

logger = logging.getLogger(__name__)

SPORT_NAMES = {
    "table_tennis": "Настольный теннис",
    "tennis": "Теннис",
}


def _upsert_match(db: Session, core: dict, raw: dict) -> Match:
    row = db.query(Match).filter(Match.external_id == core["external_id"]).one_or_none()
    if row is None:
        row = Match(external_id=core["external_id"])
        db.add(row)
    row.sport = core["sport"]
    row.status = core["status"]
    row.tournament_id = core.get("tournament_id") or None
    row.tournament_name = core.get("tournament") or ""
    row.player_a_id = core.get("player_a_id") or ""
    row.player_b_id = core.get("player_b_id") or ""
    row.player_a = core.get("player_a") or ""
    row.player_b = core.get("player_b") or ""
    row.scheduled_at = core.get("scheduled_at")
    row.winner = core.get("winner")
    row.score_display = core.get("score_display")
    extra_odds = parser.parse_extra_odds(raw)
    row.raw = {
        "id": raw.get("id"),
        "status": raw.get("status"),
        "winner": raw.get("winner"),
        "hasBkOdds": raw.get("hasBkOdds"),
        "pregame": raw.get("pregame"),
        "tennis": raw.get("tennis"),
        "best_of": core.get("best_of"),
        "extra_odds": extra_odds,
    }
    row.updated_at = utcnow()
    db.flush()
    return row


def _save_stats(db: Session, match_id: int, stats: dict) -> None:
    if not stats:
        return
    existing = (
        db.query(MatchStat)
        .filter(MatchStat.match_id == match_id, MatchStat.period == "ALL")
        .one_or_none()
    )
    if existing is None:
        existing = MatchStat(match_id=match_id, period="ALL")
        db.add(existing)
    existing.features = stats.get("features")
    existing.serve_win_pct_a = stats.get("serve_win_pct_a")
    existing.serve_win_pct_b = stats.get("serve_win_pct_b")
    existing.receive_win_pct_a = stats.get("receive_win_pct_a")
    existing.receive_win_pct_b = stats.get("receive_win_pct_b")
    existing.points_a = stats.get("points_a")
    existing.points_b = stats.get("points_b")


def _save_prediction(db: Session, match_row: Match, payload: dict) -> Prediction:
    row = (
        db.query(Prediction)
        .filter(Prediction.external_id == payload["external_id"], Prediction.settled.is_(False))
        .order_by(Prediction.id.desc())
        .first()
    )
    if row is None:
        row = Prediction(external_id=payload["external_id"])
        db.add(row)
    row.match_id = match_row.id
    row.sport = payload["sport"]
    row.sport_name = payload.get("sport_name") or SPORT_NAMES.get(payload["sport"], payload["sport"])
    row.tournament = payload.get("tournament") or ""
    sched = payload.get("scheduled_at")
    if isinstance(sched, datetime):
        row.scheduled_at = sched
    elif isinstance(sched, str) and sched:
        try:
            dt = datetime.fromisoformat(sched.replace("Z", "+00:00"))
            row.scheduled_at = dt.replace(tzinfo=None) if dt.tzinfo else dt
        except ValueError:
            pass
    row.player_a = payload["player_a"]
    row.player_b = payload["player_b"]
    row.player_a_elo = payload["player_a_elo"]
    row.player_b_elo = payload["player_b_elo"]
    row.elo_matches_a = payload.get("elo_matches_a") or 0
    row.elo_matches_b = payload.get("elo_matches_b") or 0
    row.odds_a = payload.get("odds_a")
    row.odds_b = payload.get("odds_b")
    row.prob_a = payload["prob_a"]
    row.prob_b = payload["prob_b"]
    row.predicted_winner = payload["predicted_winner"]
    row.bookmaker = payload.get("bookmaker")
    row.is_value = payload["is_value"]
    row.is_signal = payload["is_signal"]
    row.edge = payload["edge"]
    row.kelly_fraction = payload["kelly_fraction"]
    row.confidence = payload["confidence"]
    row.verdict = payload["verdict"]
    row.forecast_source = payload.get("forecast_source")
    row.risk_tier = payload.get("risk_tier")
    row.model_diagnostics = payload.get("model_diagnostics")
    row.h2h_home_wins = payload.get("h2h_home_wins")
    row.h2h_away_wins = payload.get("h2h_away_wins")
    row.ai_analysis = payload.get("ai_analysis")
    db.flush()
    return row


def _settle(db: Session, finished: list[dict], sport: str) -> int:
    by_id = {str(item.get("id")): item for item in finished}
    open_rows = (
        db.query(Prediction)
        .filter(Prediction.sport == sport, Prediction.settled.is_(False))
        .all()
    )
    closed = 0
    for pred in open_rows:
        raw = by_id.get(pred.external_id)
        if not raw or raw.get("status") != "finished":
            continue
        winner = raw.get("winner")
        if winner not in {"home", "away"}:
            continue
        actual = pred.player_a if winner == "home" else pred.player_b
        pick_is_home = pred.predicted_winner == pred.player_a
        won = (winner == "home" and pick_is_home) or (winner == "away" and not pick_is_home)
        stake_odds = pred.odds_a if pick_is_home else pred.odds_b
        if won and stake_odds:
            pnl = round(stake_odds - 1.0, 4)
        elif won:
            pnl = 1.0
        else:
            pnl = -1.0
        history = PredictionHistory(
            prediction_id=pred.id,
            external_id=pred.external_id,
            result="win" if won else "loss",
            actual_winner=actual,
            profit_loss=pnl,
            roi=pnl,
        )
        db.add(history)
        pred.settled = True
        closed += 1
    return closed


def _ingest_finished_list(db: Session, sport: str, matches: list[dict]) -> int:
    count = 0
    seen: set[str] = set()
    for raw in matches:
        if (raw.get("status") or "") != "finished":
            continue
        core = parser.parse_core(raw, sport)
        ext = core["external_id"]
        if not ext or ext in seen:
            continue
        seen.add(ext)
        match_row = _upsert_match(db, core, raw)
        _save_stats(db, match_row.id, parser.parse_stats(raw))
        count += 1
    return count


async def refresh_sport(db: Session, sport: str, *, line_only: bool = False) -> dict:
    client = APISportClient()
    kr_today = datetime.now(timezone(timedelta(hours=7))).date()
    upcoming, line_stats = await client.fetch_line(
        sport,
        date_from=kr_today.isoformat(),
        date_to=(kr_today + timedelta(days=2)).isoformat(),
        bookmaker_ids="melbet,betboom,marathon,pari",
    )
    upcoming = sorted(upcoming, key=lambda m: m.get("startTimestamp") or 0)

    player_ids: list[str] = []
    for raw in upcoming:
        core = parser.parse_core(raw, sport)
        player_ids.append(core.get("player_a_id") or "")
        player_ids.append(core.get("player_b_id") or "")

    logger.info(
        "refresh %s: upcoming=%s (prematch=%s live=%s api_total=%s pages=%s), history_fetch=%s, line_only=%s",
        sport,
        len(upcoming),
        line_stats.get("prematch", 0),
        line_stats.get("live", 0),
        line_stats.get("api_total", 0),
        line_stats.get("pages", 0),
        settings.fetch_player_history,
        line_only,
    )
    history: list[dict] = []
    recent: list[dict] = []
    snap = quota.snapshot()
    if line_only:
        logger.info("Только линия, историю и сыгранные не трогаю")
    elif settings.fetch_player_history:
        singles_ids: list[str] = []
        seen_ids: set[str] = set()
        for raw in upcoming:
            core = parser.parse_core(raw, sport)
            for pid, name in (
                (core.get("player_a_id") or "", core.get("player_a") or ""),
                (core.get("player_b_id") or "", core.get("player_b") or ""),
            ):
                if not pid or pid in seen_ids or "/" in name:
                    continue
                seen_ids.add(pid)
                row = elo.get_or_create_player(db, sport, pid, name)
                if (row.matches_count or 0) < 8:
                    singles_ids.append(pid)
        cap = max(0, min(int(settings.api_sport_history_players_per_refresh), snap["remaining"] - 1))
        if cap <= 0:
            logger.info("История игроков пропущена: квота %s/%s", snap["used"], snap["budget"])
        else:
            logger.info("Тяну историю %s игроков из %s", cap, len(singles_ids))
            history = await client.player_histories(sport, singles_ids[:cap], page_size=30)
    elif snap["remaining"] >= 2:
        recent = await client.get_matches(
            sport,
            status="finished",
            page=1,
            page_size=50,
            with_pregame=False,
            with_bk_odds=False,
            has_bk_odds=None,
        )
    else:
        logger.info("Сыгранные для Elo пропущены: квота %s/%s", snap["used"], snap["budget"])
    ingested = _ingest_finished_list(db, sport, history + recent)
    replayed = 0 if line_only else elo.rebuild_sport_elo(db, sport)
    closed = _settle(db, history + recent, sport)

    built: list[tuple] = []
    for raw in upcoming:
        core = parser.parse_core(raw, sport)
        match_row = _upsert_match(db, core, raw)
        odds = parser.parse_odds(raw)
        h2h_home, h2h_away = parser.parse_h2h(raw)
        signals = parser.parse_match_signals(raw, sport)
        elo_a, elo_b, n_a, n_b = elo.ratings(
            db,
            sport,
            core["player_a_id"],
            core["player_a"],
            core["player_b_id"],
            core["player_b"],
        )
        form_a = elo.recent_winrate(db, sport, core["player_a_id"])
        form_b = elo.recent_winrate(db, sport, core["player_b_id"])
        payload = build_prediction(
            core=core,
            odds=odds,
            elo_a=elo_a,
            elo_b=elo_b,
            matches_a=n_a,
            matches_b=n_b,
            h2h_home=h2h_home,
            h2h_away=h2h_away,
            form_a=form_a,
            form_b=form_b,
            signals=signals,
        )
        payload["extra_odds"] = parser.parse_extra_odds(raw)
        payload["extra_bets"] = extra_bet_hints(payload)
        built.append((match_row, payload))

    value_rows = sorted(
        [item for item in built if item[1]["is_value"]],
        key=lambda pair: pair[1]["edge"],
        reverse=True,
    )
    for index, (_match_row, payload) in enumerate(value_rows):
        payload["is_signal"] = index < settings.max_daily_signals

    ai_count = 0
    ai_ids: set[int] = set()
    if not line_only:
        ai_cap = max(int(settings.max_daily_signals), int(settings.deepseek_thin_cap), 12)
        thin = [item for item in built if deepseek.needs_research(item[1])]
        solid = [
            item
            for item in built
            if not deepseek.needs_research(item[1]) and "/" not in (item[1].get("player_a") or "")
        ]
        thin.sort(key=lambda pair: ("/" in (pair[1].get("player_a") or ""), pair[1].get("scheduled_at") or ""))
        solid.sort(key=lambda pair: (not pair[1]["is_value"], -abs((pair[1].get("prob_a") or 0.5) - 0.5)))
        ordered = thin[: settings.deepseek_thin_cap] + solid[: max(0, ai_cap - min(len(thin), settings.deepseek_thin_cap))]
        ai_ids = {id(payload) for _row, payload in ordered}
    for match_row, payload in built:
        if id(payload) in ai_ids:
            if deepseek.needs_research(payload):
                analysis = await deepseek.research_forecast(payload)
                if analysis:
                    deepseek.overlay_forecast(payload, analysis)
                    analysis["extra_bets"] = payload.get("extra_bets")
                    payload["ai_analysis"] = analysis
                    ai_count += 1
            else:
                analysis = await deepseek.analyze_match(payload)
                if analysis:
                    analysis["extra_bets"] = merge_extra_bets(
                        payload.get("extra_bets") or [],
                        analysis.get("extra_bets"),
                        payload.get("extra_odds") or [],
                    )
                    payload["ai_analysis"] = analysis
                    ai_count += 1
        _save_prediction(db, match_row, payload)

    db.commit()
    return {
        "status": "success",
        "sport": sport,
        "upcoming": len(built),
        "prematch": line_stats.get("prematch", 0),
        "live": line_stats.get("live", 0),
        "api_total": line_stats.get("api_total", 0),
        "api_pages": line_stats.get("pages", 0),
        "with_odds": sum(1 for _m, p in built if p.get("odds_a")),
        "value_bets": sum(1 for _m, p in built if p["is_value"]),
        "signals": sum(1 for _m, p in built if p["is_signal"]),
        "ai_analyzed": ai_count,
        "finished_for_elo": ingested,
        "elo_replayed": replayed,
        "settled": closed,
    }


def _safe_ai(ai, scored: dict):
    extra = scored.get("extra_bets") or []
    if not ai:
        return {"extra_bets": extra} if extra else None
    winner = scored.get("predicted_winner") or ""
    rec = str((ai or {}).get("recommendation") or "")
    out = dict(ai)
    if extra:
        out["extra_bets"] = extra
    if scored.get("verdict") == "ai_research" or scored.get("forecast_source") in {"deepseek", "deepseek_overlay"}:
        if extra:
            out["extra_bets"] = extra
        return out
    if not scored.get("is_value"):
        if not extra and not out.get("analysis"):
            return None
        return {"analysis": out.get("analysis"), "extra_bets": extra or out.get("extra_bets") or []}
    if winner and rec and winner not in rec:
        out.pop("recommendation", None)
    return out


def _match_raw(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw


def prediction_to_dict(row: Prediction, match_raw=None, match_row: Match | None = None) -> dict:
    matches_a = getattr(row, "elo_matches_a", 0) or 0
    matches_b = getattr(row, "elo_matches_b", 0) or 0
    elo_a = row.player_a_elo or 1500
    elo_b = row.player_b_elo or 1500
    if row.sport == "tennis":
        from app.services.historical_elo import blend

        elo_a, matches_a = blend(row.player_a, elo_a, matches_a)
        elo_b, matches_b = blend(row.player_b, elo_b, matches_b)
    odds = None
    if row.odds_a and row.odds_b:
        odds = {"odds_a": row.odds_a, "odds_b": row.odds_b, "bookmaker": row.bookmaker}
    raw = _match_raw(match_raw)
    signals = parser.parse_match_signals(raw, row.sport)
    extra_odds = parser.parse_extra_odds(raw)
    scored = build_prediction(
        core={
            "external_id": row.external_id,
            "sport": row.sport,
            "sport_name": row.sport_name,
            "tournament": row.tournament,
            "scheduled_at": row.scheduled_at,
            "player_a": row.player_a,
            "player_b": row.player_b,
        },
        odds=odds,
        elo_a=elo_a,
        elo_b=elo_b,
        matches_a=matches_a,
        matches_b=matches_b,
        h2h_home=row.h2h_home_wins,
        h2h_away=row.h2h_away_wins,
        signals=signals,
    )
    scored["extra_odds"] = extra_odds
    hints = extra_bet_hints(scored, extra_odds)
    ai_bets = (row.ai_analysis or {}).get("extra_bets") if isinstance(row.ai_analysis, dict) else []
    extra_bets = merge_extra_bets(hints, ai_bets, extra_odds)
    scored["extra_bets"] = extra_bets
    persisted_forecast_source = getattr(row, "forecast_source", None) or scored.get("forecast_source")
    persisted_risk_tier = getattr(row, "risk_tier", None) or scored.get("risk_tier")
    persisted_model_diagnostics = getattr(row, "model_diagnostics", None)
    if not isinstance(persisted_model_diagnostics, dict):
        persisted_model_diagnostics = scored.get("model_diagnostics")
    scored["forecast_source"] = persisted_forecast_source
    scored["risk_tier"] = persisted_risk_tier
    scored["model_diagnostics"] = persisted_model_diagnostics
    ai = row.ai_analysis if isinstance(row.ai_analysis, dict) else None
    if ai and ai.get("forecast_used"):
        deepseek.overlay_forecast(scored, ai)
        extra_bets = scored.get("extra_bets") or extra_bets
    return {
        "id": row.id,
        "match_id": row.external_id,
        "external_id": row.external_id,
        "sport": row.sport,
        "sport_name": row.sport_name,
        "tournament": row.tournament,
        "scheduled_at": row.scheduled_at.isoformat() if isinstance(row.scheduled_at, datetime) else row.scheduled_at,
        "player_a": row.player_a,
        "player_b": row.player_b,
        "player_a_elo": round(elo_a, 1),
        "player_b_elo": round(elo_b, 1),
        "elo_matches_a": matches_a,
        "elo_matches_b": matches_b,
        "odds_a": row.odds_a,
        "odds_b": row.odds_b,
        "prob_a": scored["prob_a"],
        "prob_b": scored["prob_b"],
        "predicted_winner": scored["predicted_winner"],
        "bookmaker": row.bookmaker,
        "is_value": scored["is_value"],
        "is_signal": bool(row.is_signal) and scored["is_value"],
        "edge": scored["edge"],
        "kelly_fraction": scored["kelly_fraction"],
        "confidence": scored["confidence"],
        "verdict": scored["verdict"],
        "forecast_source": scored.get("forecast_source"),
        "risk_tier": scored.get("risk_tier"),
        "model_diagnostics": scored.get("model_diagnostics"),
        "h2h_home_wins": row.h2h_home_wins,
        "h2h_away_wins": row.h2h_away_wins,
        "seed_a": scored.get("seed_a"),
        "seed_b": scored.get("seed_b"),
        "surface": scored.get("surface"),
        "best_of": scored.get("best_of"),
        "streak_wins_a": scored.get("streak_wins_a") or 0,
        "streak_wins_b": scored.get("streak_wins_b") or 0,
        "extra_odds": extra_odds,
        "extra_bets": extra_bets,
        "ai_analysis": _safe_ai(row.ai_analysis, {**scored, "extra_bets": extra_bets}),
        "match_status": (match_row.status if match_row else None) or (raw.get("status") if raw else None),
        "score_display": match_row.score_display if match_row else None,
        "source": "api_sport",
    }


async def enrich_thin_predictions(db: Session, sport: str, *, limit: int | None = None, force: bool = False) -> dict:
    """DeepSeek-исследование слабых матчей без запросов к API-Sport."""
    cap = int(limit if limit is not None else settings.deepseek_thin_cap)
    cap = max(1, min(cap, 20))
    rows = (
        db.query(Prediction)
        .filter(Prediction.sport == sport, Prediction.settled.is_(False))
        .order_by(Prediction.scheduled_at.asc())
        .all()
    )
    raw_by_id = {}
    ext_ids = [row.external_id for row in rows]
    if ext_ids:
        for match in db.query(Match).filter(Match.external_id.in_(ext_ids)).all():
            raw_by_id[match.external_id] = match.raw
    done = 0
    skipped = 0
    for row in rows:
        payload = prediction_to_dict(row, match_raw=raw_by_id.get(row.external_id))
        ai = row.ai_analysis if isinstance(row.ai_analysis, dict) else {}
        if ai.get("forecast_used") and not force:
            skipped += 1
            continue
        if not deepseek.needs_research(payload) and not force:
            skipped += 1
            continue
        if done >= cap:
            break
        analysis = await deepseek.research_forecast(payload)
        if not analysis:
            continue
        deepseek.overlay_forecast(payload, analysis)
        analysis["extra_bets"] = payload.get("extra_bets")
        payload["ai_analysis"] = analysis
        match_row = db.query(Match).filter(Match.external_id == row.external_id).one_or_none()
        if match_row is None:
            continue
        _save_prediction(db, match_row, payload)
        done += 1
    db.commit()
    return {
        "status": "success",
        "sport": sport,
        "enriched": done,
        "skipped": skipped,
        "cap": cap,
        "note": "API-Sport не вызывался. Это не value.",
    }
