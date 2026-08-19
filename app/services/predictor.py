from __future__ import annotations

import math

from app.core.config import settings
from app.services.elo import expected_score
from app.services.markets import extra_bet_hints, merge_extra_bets

__all__ = [
    "build_prediction",
    "extra_bet_hints",
    "merge_extra_bets",
    "expected_score",
]

MIN_ELO_EACH = 8
MIN_H2H = 8
MIN_H2H_TENNIS = 4
MAX_VALUE_ODDS_TENNIS = 3.2


def _clip(value: float, low: float = 0.12, high: float = 0.88) -> float:
    return max(low, min(high, value))


def _shrink_to_coin(prob: float, amount: float) -> float:
    amount = max(0.0, min(0.45, amount))
    return 0.5 + (prob - 0.5) * (1.0 - amount)


def h2h_total(home_wins: int | None, away_wins: int | None) -> int:
    return (home_wins or 0) + (away_wins or 0)


def seed_to_rating(seed: int | None, *, unseeded: float = 45.0) -> float:
    rank = float(seed) if seed and seed > 0 else unseeded
    return 1860.0 - 55.0 * math.log(rank)


def tennis_seed_prob(seed_a: int | None, seed_b: int | None) -> float | None:
    if seed_a is None or seed_b is None:
        return None
    return expected_score(seed_to_rating(seed_a), seed_to_rating(seed_b))


def blend_streaks(prob: float, wins_a: int, wins_b: int, losses_a: int, losses_b: int) -> float:
    shift = 0.012 * (wins_a - wins_b) - 0.01 * (losses_a - losses_b)
    return _clip(prob + max(-0.07, min(0.07, shift)))


def has_enough_sample(
    matches_a: int,
    matches_b: int,
    home_wins: int | None,
    away_wins: int | None,
    *,
    sport: str = "",
    seed_prob: float | None = None,
    is_doubles: bool = False,
) -> bool:
    if matches_a >= MIN_ELO_EACH and matches_b >= MIN_ELO_EACH:
        return True
    if sport == "tennis":
        if seed_prob is not None:
            return True
        if not is_doubles and max(matches_a, matches_b) >= MIN_ELO_EACH and min(matches_a, matches_b) >= 3:
            return True
        return h2h_total(home_wins, away_wins) >= MIN_H2H_TENNIS
    return h2h_total(home_wins, away_wins) >= MIN_H2H


def enough_for_value(
    *,
    sport: str,
    is_doubles: bool,
    seed_a: int | None,
    seed_b: int | None,
    matches_a: int,
    matches_b: int,
    home_wins: int | None,
    away_wins: int | None,
) -> bool:
    if matches_a >= MIN_ELO_EACH and matches_b >= MIN_ELO_EACH:
        return True
    if sport == "tennis":
        if seed_a is not None and seed_b is not None:
            return True
        return h2h_total(home_wins, away_wins) >= MIN_H2H_TENNIS
    return h2h_total(home_wins, away_wins) >= MIN_H2H


def blend_h2h(elo_prob: float, home_wins: int | None, away_wins: int | None, sample: int) -> float:
    if home_wins is None or away_wins is None:
        return elo_prob
    total = home_wins + away_wins
    if total < 1:
        return elo_prob
    h2h = home_wins / total
    if sample < 6:
        if total >= MIN_H2H:
            weight = min(0.70, 0.38 + 0.04 * total)
        else:
            weight = min(0.28, 0.08 + 0.04 * total)
    else:
        weight = min(0.28, 0.04 * total + 0.08)
    return _clip(elo_prob * (1 - weight) + h2h * weight)


def blend_form(prob: float, form_a: float | None, form_b: float | None) -> float:
    if form_a is None and form_b is None:
        return prob
    fa = 0.5 if form_a is None else form_a
    fb = 0.5 if form_b is None else form_b
    return _clip(prob + (fa - fb) * 0.16)


def is_womens_tennis(tournament: str, sport: str) -> bool:
    if sport != "tennis":
        return False
    text = (tournament or "").casefold()
    markers = (" women", " women.", " wta", " girls", " female", " women singles", " women doubles")
    return any(marker in text for marker in markers)


def context_compression(
    *,
    sport: str,
    tournament: str,
    matches_a: int,
    matches_b: int,
    home_wins: int | None,
    away_wins: int | None,
    seed_a: int | None,
    seed_b: int | None,
    surface: str | None,
    base_prob: float,
    prob: float,
) -> tuple[float, list[str]]:
    flags: list[str] = []
    sample_min = min(matches_a, matches_b)
    sample_gap = abs(matches_a - matches_b)
    h2h_sample = h2h_total(home_wins, away_wins)
    compression = 0.0

    if sport == "tennis" and sample_min < MIN_ELO_EACH and h2h_sample == 0 and (seed_a is None or seed_b is None):
        compression += 0.18
        flags.append("elo_only_signal")
    if sample_min < 5:
        compression += 0.07
        flags.append("very_sparse_sample")
    if sample_gap >= 8:
        compression += 0.05
        flags.append("wide_sample_gap")
    if sport == "tennis" and not surface:
        compression += 0.04
        flags.append("missing_surface_context")
    if is_womens_tennis(tournament, sport) and sample_min < 12:
        compression += 0.08
        flags.append("womens_match_volatility")
    if abs(prob - base_prob) < 0.02 and sample_min < MIN_ELO_EACH:
        compression += 0.04
        flags.append("low_context_confirmation")

    if compression <= 0:
        return prob, flags
    return _clip(_shrink_to_coin(prob, compression)), flags


def implied_probs(odds_a: float, odds_b: float) -> tuple[float, float]:
    raw_a = 1.0 / odds_a
    raw_b = 1.0 / odds_b
    total = raw_a + raw_b
    return raw_a / total, raw_b / total


def kelly(prob: float, odds: float) -> float:
    if odds <= 1:
        return 0.0
    fraction = (prob * odds - 1.0) / (odds - 1.0)
    return round(max(0.0, fraction), 4)


def tournament_risk_flags(
    *,
    matches_a: int,
    matches_b: int,
    home_wins: int | None,
    away_wins: int | None,
    seed_a: int | None,
    seed_b: int | None,
    sport: str,
    is_doubles: bool,
    tournament: str = "",
    surface: str | None = None,
    edge: float | None = None,
) -> list[str]:
    flags: list[str] = []
    sample_min = min(matches_a, matches_b)
    sample_gap = abs(matches_a - matches_b)
    h2h_sample = h2h_total(home_wins, away_wins)

    if sample_min < MIN_ELO_EACH:
        flags.append("sparse_sample")
    if sample_gap >= 6:
        flags.append("asymmetric_sample")
    if h2h_sample == 0:
        flags.append("no_h2h_history")
    elif sport != "tennis" and h2h_sample < MIN_H2H:
        flags.append("limited_h2h_history")
    elif sport == "tennis" and h2h_sample < MIN_H2H_TENNIS:
        flags.append("limited_h2h_history")
    if sport == "tennis" and (seed_a is None or seed_b is None):
        flags.append("missing_seed_context")
    if sport == "tennis" and not surface:
        flags.append("missing_surface_context")
    if is_womens_tennis(tournament, sport) and sample_min < 12:
        flags.append("womens_match_volatility")
    if is_doubles:
        flags.append("doubles_variance")
    if edge is not None and edge < max(0.0125, settings.min_edge_threshold * 0.75):
        flags.append("thin_edge")
    return flags


def overall_risk_profile(flags: list[str]) -> tuple[str, float]:
    weights = {
        "sparse_sample": 0.34,
        "asymmetric_sample": 0.18,
        "no_h2h_history": 0.12,
        "limited_h2h_history": 0.08,
        "missing_seed_context": 0.1,
        "missing_surface_context": 0.08,
        "womens_match_volatility": 0.12,
        "doubles_variance": 0.1,
        "thin_edge": 0.2,
        "elo_only_signal": 0.18,
        "very_sparse_sample": 0.12,
        "wide_sample_gap": 0.08,
        "low_context_confirmation": 0.08,
    }
    risk_score = sum(weights.get(flag, 0.0) for flag in flags)
    risk_score = round(min(1.0, risk_score), 3)
    if risk_score >= 0.55:
        risk_tier = "high"
    elif risk_score >= 0.28:
        risk_tier = "medium"
    else:
        risk_tier = "low"
    return risk_tier, risk_score


def calibrated_confidence(
    *,
    prob: float,
    matches_a: int,
    matches_b: int,
    home_wins: int | None,
    away_wins: int | None,
    risk_score: float,
    edge: float | None = None,
) -> float:
    margin = abs(prob - 0.5)
    sample_min = min(matches_a, matches_b)
    sample_gap = abs(matches_a - matches_b)
    h2h_sample = h2h_total(home_wins, away_wins)

    confidence = 0.5 + min(0.18, margin * 0.45)
    confidence += min(0.045, sample_min * 0.0045)
    confidence += min(0.025, h2h_sample * 0.003)
    confidence -= min(0.06, sample_gap * 0.005)

    if margin < 0.035:
        confidence -= 0.075
    elif margin < 0.06:
        confidence -= 0.05

    if edge is not None:
        if edge < settings.min_edge_threshold:
            confidence -= 0.06
        elif edge < settings.min_edge_threshold + 0.02:
            confidence -= 0.035

    confidence -= risk_score * 0.21
    return round(_clip(confidence, low=0.5, high=0.76), 3)


def build_prediction(
    *,
    core: dict,
    odds: dict | None,
    elo_a: float,
    elo_b: float,
    matches_a: int,
    matches_b: int,
    h2h_home: int | None,
    h2h_away: int | None,
    form_a: float | None = None,
    form_b: float | None = None,
    signals: dict | None = None,
    min_odds: float | None = None,
    min_edge: float | None = None,
) -> dict:
    min_odds = settings.min_odds_threshold if min_odds is None else min_odds
    min_edge = settings.min_edge_threshold if min_edge is None else min_edge
    signals = signals or {}
    sport = core.get("sport") or ""

    sample = min(matches_a, matches_b)
    is_doubles = "/" in (core.get("player_a") or "") or "/" in (core.get("player_b") or "")
    seed_a = signals.get("seed_a")
    seed_b = signals.get("seed_b")
    surface = signals.get("surface")
    seed_p = tennis_seed_prob(seed_a, seed_b) if sport == "tennis" else None
    base_prob = expected_score(elo_a, elo_b)
    after_seed = base_prob
    if sport == "tennis" and seed_p is not None and sample < MIN_ELO_EACH:
        after_seed = seed_p
    else:
        if sport == "tennis" and seed_p is not None:
            after_seed = 0.55 * base_prob + 0.45 * seed_p

    after_h2h = blend_h2h(after_seed, h2h_home, h2h_away, sample)
    after_streaks = after_h2h
    if sport == "tennis":
        after_streaks = blend_streaks(
            after_h2h,
            int(signals.get("wins_a") or 0),
            int(signals.get("wins_b") or 0),
            int(signals.get("losses_a") or 0),
            int(signals.get("losses_b") or 0),
        )
    prob_a = blend_form(after_streaks, form_a, form_b)
    prob_a, compression_flags = context_compression(
        sport=sport,
        tournament=core.get("tournament") or "",
        matches_a=matches_a,
        matches_b=matches_b,
        home_wins=h2h_home,
        away_wins=h2h_away,
        seed_a=seed_a,
        seed_b=seed_b,
        surface=surface,
        base_prob=base_prob,
        prob=prob_a,
    )
    prob_a = round(_clip(prob_a), 4)
    prob_b = round(1.0 - prob_a, 4)

    predicted = core["player_a"] if prob_a >= 0.5 else core["player_b"]
    has_forecast = has_enough_sample(
        matches_a,
        matches_b,
        h2h_home,
        h2h_away,
        sport=sport,
        seed_prob=seed_p,
        is_doubles=is_doubles,
    )
    can_value = enough_for_value(
        sport=sport,
        is_doubles=is_doubles,
        seed_a=seed_a,
        seed_b=seed_b,
        matches_a=matches_a,
        matches_b=matches_b,
        home_wins=h2h_home,
        away_wins=h2h_away,
    )
    risk_flags = tournament_risk_flags(
        matches_a=matches_a,
        matches_b=matches_b,
        home_wins=h2h_home,
        away_wins=h2h_away,
        seed_a=seed_a,
        seed_b=seed_b,
        sport=sport,
        is_doubles=is_doubles,
        tournament=core.get("tournament") or "",
        surface=surface,
    )
    risk_flags.extend(flag for flag in compression_flags if flag not in risk_flags)
    risk_tier, risk_score = overall_risk_profile(risk_flags)
    confidence = calibrated_confidence(
        prob=prob_a,
        matches_a=matches_a,
        matches_b=matches_b,
        home_wins=h2h_home,
        away_wins=h2h_away,
        risk_score=risk_score,
    )
    if not has_forecast:
        confidence = round(min(confidence, 0.56), 3)

    model_is_coin = abs(prob_a - 0.5) < 0.08
    model_diagnostics = {
        "base_prob": round(_clip(base_prob), 4),
        "after_seed": round(_clip(after_seed), 4),
        "after_h2h": round(_clip(after_h2h), 4),
        "after_streaks": round(_clip(after_streaks), 4),
        "after_form": prob_a,
        "seed_prob": round(_clip(seed_p), 4) if seed_p is not None else None,
        "sample_min": sample,
        "sample_gap": abs(matches_a - matches_b),
        "h2h_sample": h2h_total(h2h_home, h2h_away),
        "has_forecast": has_forecast,
        "can_value": can_value,
        "model_is_coin": model_is_coin,
        "compression_flags": list(compression_flags),
        "risk_flags": list(risk_flags),
    }

    payload = {
        **core,
        "player_a_elo": round(elo_a, 1),
        "player_b_elo": round(elo_b, 1),
        "elo_matches_a": matches_a,
        "elo_matches_b": matches_b,
        "prob_a": prob_a,
        "prob_b": prob_b,
        "predicted_winner": predicted,
        "odds_a": None,
        "odds_b": None,
        "bookmaker": None,
        "edge": 0.0,
        "kelly_fraction": 0.0,
        "confidence": confidence,
        "forecast_source": "model",
        "risk_tier": risk_tier,
        "is_value": False,
        "is_signal": False,
        "verdict": "no_odds",
        "h2h_home_wins": h2h_home,
        "h2h_away_wins": h2h_away,
        "form_a": form_a,
        "form_b": form_b,
        "seed_a": signals.get("seed_a"),
        "seed_b": signals.get("seed_b"),
        "surface": signals.get("surface"),
        "best_of": signals.get("best_of"),
        "streak_wins_a": signals.get("wins_a") or 0,
        "streak_wins_b": signals.get("wins_b") or 0,
        "ai_analysis": None,
        "extra_odds": [],
        "extra_bets": [],
        "model_diagnostics": model_diagnostics,
        "source": "api_sport",
    }

    if not odds:
        if not has_forecast:
            payload["verdict"] = "insufficient_data"
            payload["predicted_winner"] = ""
        return payload

    odds_a = float(odds["odds_a"])
    odds_b = float(odds["odds_b"])
    payload["odds_a"] = odds_a
    payload["odds_b"] = odds_b
    payload["bookmaker"] = odds.get("bookmaker")

    book_a, _book_b = implied_probs(odds_a, odds_b)
    pick_is_home = prob_a >= 0.5
    pick_prob = prob_a if pick_is_home else prob_b
    pick_odds = odds_a if pick_is_home else odds_b
    pick_book = book_a if pick_is_home else (1.0 - book_a)
    edge = round(pick_prob - pick_book, 4)
    risk_flags = tournament_risk_flags(
        matches_a=matches_a,
        matches_b=matches_b,
        home_wins=h2h_home,
        away_wins=h2h_away,
        seed_a=seed_a,
        seed_b=seed_b,
        sport=sport,
        is_doubles=is_doubles,
        tournament=core.get("tournament") or "",
        surface=surface,
        edge=edge,
    )
    risk_flags.extend(flag for flag in compression_flags if flag not in risk_flags)
    risk_tier, risk_score = overall_risk_profile(risk_flags)
    payload["edge"] = edge
    payload["kelly_fraction"] = kelly(pick_prob, pick_odds)
    payload["risk_tier"] = risk_tier
    payload["confidence"] = calibrated_confidence(
        prob=prob_a,
        matches_a=matches_a,
        matches_b=matches_b,
        home_wins=h2h_home,
        away_wins=h2h_away,
        risk_score=risk_score,
        edge=edge,
    )
    if not has_forecast:
        payload["confidence"] = round(min(payload["confidence"], 0.56), 3)
    payload["model_diagnostics"].update(
        {
            "risk_score": risk_score,
            "risk_flags": list(risk_flags),
            "market_edge": edge,
            "pick_prob": round(pick_prob, 4),
            "pick_book_prob": round(pick_book, 4),
            "pick_odds": round(pick_odds, 4),
        }
    )

    if not has_forecast:
        payload["verdict"] = "insufficient_data"
        payload["kelly_fraction"] = 0.0
        payload["edge"] = 0.0
        payload["predicted_winner"] = ""
    elif model_is_coin or not can_value:
        payload["verdict"] = "no_value"
        payload["kelly_fraction"] = 0.0
        if not can_value:
            payload["edge"] = 0.0
    elif sport == "tennis" and pick_odds > MAX_VALUE_ODDS_TENNIS:
        payload["verdict"] = "no_value"
        payload["kelly_fraction"] = 0.0
    elif pick_odds >= min_odds and edge >= min_edge:
        payload["is_value"] = True
        payload["verdict"] = "value_bet"
    else:
        payload["verdict"] = "no_value"
    return payload
