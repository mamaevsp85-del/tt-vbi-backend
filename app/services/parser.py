from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

BK_PRIORITY = ("melbet", "betboom", "marathon", "pari")


def _team_name(team: dict | None) -> str:
    if not team:
        return ""
    return (team.get("name") or team.get("fullName") or "").strip()


def _team_id(team: dict | None) -> str:
    if not team or team.get("id") is None:
        return ""
    return str(team.get("id"))


def _ts_to_dt(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        ms = int(value)
        if ms > 10_000_000_000:
            ms = ms / 1000
        return datetime.fromtimestamp(ms, tz=timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        return None


def parse_core(match: dict[str, Any], sport: str) -> dict[str, Any]:
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}
    tournament = match.get("tournament") or {}
    home_score = match.get("homeScore") or {}
    away_score = match.get("awayScore") or {}
    tennis = match.get("tennis") or {}

    score_display = home_score.get("display") or away_score.get("display")
    if not score_display and home_score.get("current") is not None:
        score_display = f"{home_score.get('current')}-{away_score.get('current')}"

    return {
        "external_id": str(match.get("id", "")),
        "sport": sport,
        "sport_name": "Настольный теннис" if sport == "table_tennis" else "Теннис",
        "status": match.get("status") or "",
        "tournament_id": str(tournament.get("id") or ""),
        "tournament": tournament.get("name") or "",
        "player_a_id": _team_id(home),
        "player_b_id": _team_id(away),
        "player_a": _team_name(home) or "Игрок А",
        "player_b": _team_name(away) or "Игрок Б",
        "scheduled_at": _ts_to_dt(match.get("startTimestamp")),
        "date_event": match.get("dateEvent"),
        "winner": match.get("winner"),
        "winner_code": match.get("winnerCode"),
        "score_display": str(score_display) if score_display is not None else None,
        "ground_type": tennis.get("groundType"),
        "best_of": tennis.get("bestOf"),
        "home_seed": tennis.get("homePlayerSeed"),
        "away_seed": tennis.get("awayPlayerSeed"),
    }


def parse_h2h(match: dict[str, Any]) -> tuple[int | None, int | None]:
    pregame = match.get("pregame") or {}
    h2h = pregame.get("h2h") or {}
    duel = h2h.get("teamDuel") or {}
    home = duel.get("homeWins")
    away = duel.get("awayWins")
    if home is None and away is None:
        return None, None
    return int(home or 0), int(away or 0)


def _as_seed(value: Any) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        seed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seed if seed > 0 else None


def parse_streaks(match: dict[str, Any]) -> dict[str, int]:
    pregame = match.get("pregame") or {}
    streaks = (pregame.get("teamStreaks") or {}).get("general") or []
    out = {"wins_a": 0, "wins_b": 0, "losses_a": 0, "losses_b": 0}
    for item in streaks:
        name = str(item.get("name") or "").strip().lower()
        team = str(item.get("team") or "").strip().lower()
        try:
            value = int(str(item.get("value") or "0"))
        except (TypeError, ValueError):
            continue
        if name not in {"wins", "win", "losses", "loss"}:
            continue
        if team == "home":
            if name.startswith("win"):
                out["wins_a"] = value
            else:
                out["losses_a"] = value
        elif team == "away":
            if name.startswith("win"):
                out["wins_b"] = value
            else:
                out["losses_b"] = value
    return out


def parse_tennis_signals(match: dict[str, Any] | None) -> dict[str, Any]:
    raw = match or {}
    tennis = raw.get("tennis") or {}
    streaks = parse_streaks(raw)
    return {
        "seed_a": _as_seed(tennis.get("homePlayerSeed")),
        "seed_b": _as_seed(tennis.get("awayPlayerSeed")),
        "surface": tennis.get("groundType") or None,
        "best_of": tennis.get("bestOf") or raw.get("best_of"),
        **streaks,
    }


def parse_match_signals(match: dict[str, Any] | None, sport: str = "") -> dict[str, Any]:
    signals = parse_tennis_signals(match)
    best = signals.get("best_of")
    try:
        best = int(best) if best not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        best = None
    if best is None:
        best = 5 if sport == "table_tennis" else 3
    signals["best_of"] = best
    return signals


def _stat_items(group: dict[str, Any]) -> list[dict[str, Any]]:
    items = group.get("statisticsItems")
    if items is None:
        items = group.get("items")
    return items or []


def parse_stats(match: dict[str, Any]) -> dict[str, Any]:
    periods = match.get("matchStatistics") or []
    all_period = None
    for period in periods:
        if (period.get("period") or "").upper() == "ALL":
            all_period = period
            break
    if all_period is None and periods:
        all_period = periods[0]
    if not all_period:
        return {}

    features: dict[str, Any] = {}
    for group in all_period.get("groups") or []:
        for item in _stat_items(group):
            key = item.get("key")
            if not key:
                continue
            features[key] = {
                "home": item.get("home"),
                "away": item.get("away"),
                "home_value": item.get("homeValue"),
                "away_value": item.get("awayValue"),
                "home_total": item.get("homeTotal"),
                "away_total": item.get("awayTotal"),
            }

    def ratio(key: str, side: str) -> float | None:
        row = features.get(key) or {}
        value = row.get(f"{side}_value")
        total = row.get(f"{side}_total")
        try:
            if value is None or total in (None, 0, "0"):
                return None
            return round(float(value) / float(total), 4)
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    def number(key: str, side: str) -> float | None:
        row = features.get(key) or {}
        value = row.get(f"{side}_value")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "features": features,
        "serve_win_pct_a": ratio("servicePointsAccuracy", "home") or ratio("firstServePointsAccuracy", "home"),
        "serve_win_pct_b": ratio("servicePointsAccuracy", "away") or ratio("firstServePointsAccuracy", "away"),
        "receive_win_pct_a": ratio("receiverPointsAccuracy", "home") or ratio("firstReturnPoints", "home"),
        "receive_win_pct_b": ratio("receiverPointsAccuracy", "away") or ratio("firstReturnPoints", "away"),
        "points_a": number("pointsWon", "home") or number("pointsTotal", "home"),
        "points_b": number("pointsWon", "away") or number("pointsTotal", "away"),
    }


def _result_from_bk(odds_bk: dict[str, Any] | None) -> dict[str, Any] | None:
    if not odds_bk:
        return None
    candidates: list[dict[str, Any]] = []
    for slug in BK_PRIORITY:
        board = odds_bk.get(slug)
        if not board:
            continue
        market = (board.get("markets") or {}).get("result") or {}
        stakes = market.get("stakes") or {}
        w1 = (stakes.get("w1") or {}).get("factor")
        w2 = (stakes.get("w2") or {}).get("factor")
        try:
            odds_a = float(w1)
            odds_b = float(w2)
        except (TypeError, ValueError):
            continue
        if odds_a < 1.01 or odds_b < 1.01:
            continue
        candidates.append(
            {
                "bookmaker": slug,
                "odds_a": round(odds_a, 3),
                "odds_b": round(odds_b, 3),
                "active": bool(board.get("isBettingActive")),
                "source": "odds_bk",
            }
        )
    if not candidates:
        return None
    active = [c for c in candidates if c["active"]]
    pool = active or candidates
    return pool[0]


def _result_from_base(odds_base: list | None) -> dict[str, Any] | None:
    if not odds_base:
        return None
    market = None
    for item in odds_base:
        group = (item.get("group") or "").lower()
        name = (item.get("name") or "").lower()
        if "home/away" in group or name in {"full time", "match winner", "winner"}:
            if item.get("suspended"):
                continue
            market = item
            break
    if market is None:
        for item in odds_base:
            choices = item.get("choices") or []
            names = {str(c.get("name")) for c in choices}
            if names.issuperset({"1", "2"}):
                market = item
                break
    if not market:
        return None
    odds_a = odds_b = None
    for choice in market.get("choices") or []:
        label = str(choice.get("name") or "").strip().lower()
        decimal = choice.get("decimal")
        try:
            value = float(decimal)
        except (TypeError, ValueError):
            continue
        if label in {"1", "home", "w1", "п1"}:
            odds_a = value
        elif label in {"2", "away", "w2", "п2"}:
            odds_b = value
    if odds_a is None or odds_b is None:
        return None
    return {
        "bookmaker": "oddsBase",
        "odds_a": round(odds_a, 3),
        "odds_b": round(odds_b, 3),
        "active": not bool(market.get("suspended")),
        "source": "odds_base",
    }


def parse_odds(match: dict[str, Any]) -> dict[str, Any] | None:
    return _result_from_bk(match.get("oddsBk")) or _result_from_base(match.get("oddsBase"))


def _stake_name(stake: dict[str, Any], fallback: str) -> str:
    name = stake.get("name")
    if isinstance(name, dict):
        return str(name.get("ru") or name.get("en") or fallback)
    return fallback


def _market_name(market: dict[str, Any], fallback: str) -> str:
    name = market.get("name")
    if isinstance(name, dict):
        return str(name.get("ru") or name.get("en") or fallback)
    return fallback


def _as_factor(value: Any) -> float | None:
    try:
        factor = float(value)
    except (TypeError, ValueError):
        return None
    if factor < 1.01 or factor > 30:
        return None
    return round(factor, 3)


def _is_extra_market(slug: str, title: str) -> bool:
    return str(slug).lower() not in {"result", "1x2", "winner", "match_winner"}


def _bk_extra_lines(odds_bk: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not odds_bk:
        return []
    for slug in BK_PRIORITY:
        board = odds_bk.get(slug)
        if not board:
            continue
        markets = board.get("markets") or {}
        rows: list[dict[str, Any]] = []
        for market_key, market in markets.items():
            if market_key == "result" or not isinstance(market, dict):
                continue
            title = _market_name(market, str(market_key))
            if not _is_extra_market(str(market_key), title):
                continue
            stakes = market.get("stakes") or {}
            for side_key, stake in stakes.items():
                if not isinstance(stake, dict):
                    continue
                side_name = _stake_name(stake, str(side_key))
                lines = stake.get("lines")
                if not isinstance(lines, list) or not lines:
                    factor = _as_factor(stake.get("factor"))
                    if factor is None:
                        continue
                    lines = [{"argument": None, "factor": factor}]
                for line in lines[:10]:
                    if not isinstance(line, dict):
                        continue
                    factor = _as_factor(line.get("factor"))
                    if factor is None:
                        continue
                    rows.append(
                        {
                            "bookmaker": slug,
                            "market": str(market_key),
                            "market_name": title,
                            "side": str(side_key),
                            "side_name": side_name,
                            "line": line.get("argument"),
                            "odds": factor,
                        }
                    )
        if rows:
            return rows[:80]
    return []


def _base_extra_lines(odds_base: list | None) -> list[dict[str, Any]]:
    if not odds_base:
        return []
    rows: list[dict[str, Any]] = []
    for item in odds_base:
        if not isinstance(item, dict) or item.get("suspended"):
            continue
        group = str(item.get("group") or "")
        name = str(item.get("name") or "")
        if not _is_extra_market(group, name):
            continue
        for choice in item.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            factor = _as_factor(choice.get("decimal"))
            if factor is None:
                continue
            label = str(choice.get("name") or "").strip()
            label_l = label.lower()
            line = choice.get("handicap")
            if line is None:
                for token in label.replace(",", ".").split():
                    try:
                        line = float(token)
                        break
                    except ValueError:
                        continue
            if "over" in label_l or "больше" in label_l or label_l.startswith("тб"):
                side, side_name = "over", "ТБ"
            elif "under" in label_l or "меньше" in label_l or label_l.startswith("тм"):
                side, side_name = "under", "ТМ"
            elif label_l in {"1", "home", "w1", "п1"}:
                side, side_name = "w1", "П1"
            elif label_l in {"2", "away", "w2", "п2"}:
                side, side_name = "w2", "П2"
            else:
                side, side_name = label_l[:12], label
            rows.append(
                {
                    "bookmaker": "oddsBase",
                    "market": (group or name or "extra").lower().replace(" ", "_")[:40],
                    "market_name": name or group or "доп. рынок",
                    "side": side,
                    "side_name": side_name,
                    "line": line,
                    "odds": factor,
                }
            )
            if len(rows) >= 80:
                return rows
    return rows


def parse_extra_odds(match: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = match or {}
    cached = raw.get("extra_odds")
    if isinstance(cached, list) and cached and "oddsBk" not in raw and "oddsBase" not in raw:
        return cached
    return _bk_extra_lines(raw.get("oddsBk")) or _base_extra_lines(raw.get("oddsBase")) or (
        cached if isinstance(cached, list) else []
    )
