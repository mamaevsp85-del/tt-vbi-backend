from __future__ import annotations

import math
from typing import Any


def _clip(value: float, low: float = 0.08, high: float = 0.92) -> float:
    return max(low, min(high, value))


def wins_needed(sport: str, best_of: Any) -> int:
    try:
        bo = int(best_of) if best_of not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        bo = None
    if sport == "table_tennis":
        if bo in (5, 7, 9):
            return (bo + 1) // 2
        return 3
    if bo == 5:
        return 3
    return 2


def units_label(sport: str) -> str:
    return "партий" if sport == "table_tennis" else "сетов"


def match_win_from_set(p_set: float, need: int) -> float:
    q = 1.0 - p_set
    total = 0.0
    for lost in range(need):
        total += math.comb(need - 1 + lost, lost) * (p_set**need) * (q**lost)
    return total


def invert_set_prob(p_match: float, need: int) -> float:
    target = _clip(float(p_match))
    lo, hi = 0.05, 0.95
    for _ in range(48):
        mid = (lo + hi) / 2
        if match_win_from_set(mid, need) < target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


def score_probs(p_set: float, need: int) -> dict[str, float]:
    q = 1.0 - p_set
    out: dict[str, float] = {}
    for lost in range(need):
        out[f"{need}-{lost}"] = math.comb(need - 1 + lost, lost) * (p_set**need) * (q**lost)
        out[f"{lost}-{need}"] = math.comb(need - 1 + lost, lost) * (q**need) * (p_set**lost)
    return {key: round(val, 4) for key, val in out.items()}


def _blob(item: dict) -> str:
    return " ".join(
        str(item.get(key) or "")
        for key in ("market", "market_name", "side", "side_name")
    ).lower()


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _match_odds(extra_odds: list[dict], needles: tuple[str, ...], *, line: Any = None, side: str | None = None) -> dict | None:
    best = None
    for row in extra_odds or []:
        blob = _blob(row)
        if not any(token in blob for token in needles):
            continue
        if side and str(row.get("side") or "").lower() not in {side.lower(), str(side)}:
            side_blob = blob
            aliases = {
                "over": ("over", "больше", "тб"),
                "under": ("under", "меньше", "тм"),
                "w1": ("w1", "п1", "1"),
                "w2": ("w2", "п2", "2"),
                "odd": ("odd", "нечет", "нечёт"),
                "even": ("even", "чет", "чёт"),
            }
            if not any(a in side_blob for a in aliases.get(side.lower(), (side.lower(),))):
                continue
        if line is not None:
            got = _as_float(row.get("line"))
            want = _as_float(line)
            if got is None or want is None or abs(got - want) > 0.05:
                score_txt = f"{row.get('side')} {row.get('side_name')} {row.get('line')}".lower()
                if str(line).replace(" ", "") not in score_txt.replace(" ", "").replace(":", "-"):
                    continue
        if best is None or abs((_as_float(row.get("odds")) or 99) - 1.9) < abs((_as_float(best.get("odds")) or 99) - 1.9):
            best = row
    return best


def _hint(
    *,
    kind: str,
    market: str,
    side: str,
    why: str,
    model_prob: float | None = None,
    line: Any = None,
    book: dict | None = None,
    caution: str = "оценка из P(победы) через независимые сеты/партии, не value",
) -> dict:
    odds = book.get("odds") if book else None
    return {
        "kind": kind,
        "market": market,
        "side": side,
        "line": line if line is not None else (book.get("line") if book else None),
        "odds": odds,
        "bookmaker": book.get("bookmaker") if book else None,
        "model_prob": round(model_prob, 3) if model_prob is not None else None,
        "why": why,
        "caution": caution,
        "source": "set-model" if not book else "set-model+line",
    }


def extra_bet_hints(payload: dict, extra_odds: list | None = None) -> list[dict]:
    predicted = payload.get("predicted_winner") or ""
    if not predicted or payload.get("verdict") == "insufficient_data":
        return []
    if "/" in (payload.get("player_a") or "") or "/" in (payload.get("player_b") or ""):
        return []

    extra_odds = extra_odds if extra_odds is not None else (payload.get("extra_odds") or [])
    sport = payload.get("sport") or ""
    unit = units_label(sport)
    need = wins_needed(sport, payload.get("best_of"))
    max_sets = need * 2 - 1
    p_home = _clip(float(payload.get("prob_a") or 0.5))
    pick_home = p_home >= 0.5
    p_match = p_home if pick_home else 1.0 - p_home
    p_set = invert_set_prob(p_match, need)
    dist = score_probs(p_set, need)
    favorite = payload.get("player_a") if pick_home else payload.get("player_b")
    underdog = payload.get("player_b") if pick_home else payload.get("player_a")
    side_w = "w1" if pick_home else "w2"

    fav_scores = {f"{need}-{lost}": dist[f"{need}-{lost}"] for lost in range(need)}
    best_score, best_p = max(fav_scores.items(), key=lambda item: item[1])
    p_sweep = dist.get(f"{need}-0", 0.0)
    p_full = dist.get(f"{need}-{need - 1}", 0.0) + dist.get(f"{need - 1}-{need}", 0.0)
    p_over_main = 0.0
    main_total = need + 0.5
    for lost in range(need):
        sets = need + lost
        if sets > main_total:
            p_over_main += dist.get(f"{need}-{lost}", 0.0) + dist.get(f"{lost}-{need}", 0.0)
    p_cover_15 = sum(dist.get(f"{need}-{lost}", 0.0) for lost in range(need) if (need - lost) >= 2)

    hints: list[dict] = []

    score_book = _match_odds(
        extra_odds,
        ("score", "счёт", "счет", "correct"),
        line=best_score,
        side=side_w,
    ) or _match_odds(extra_odds, ("score", "счёт", "счет", "correct"), line=best_score)
    hints.append(
        _hint(
            kind="exact_score",
            market=f"Точный счёт {favorite} {best_score}",
            side=best_score,
            line=best_score,
            model_prob=best_p,
            why=f"самый вероятный счёт фаворита при P(сета)={p_set:.0%}",
            book=score_book,
        )
    )

    second = sorted(fav_scores.items(), key=lambda item: item[1], reverse=True)
    if len(second) > 1 and second[1][1] >= 0.18 and second[1][1] + 0.04 >= best_p:
        alt_score, alt_p = second[1]
        hints.append(
            _hint(
                kind="exact_score",
                market=f"Точный счёт {favorite} {alt_score}",
                side=alt_score,
                line=alt_score,
                model_prob=alt_p,
                why="второй по вероятности счёт — не ставить оба сразу",
            )
        )

    total_book_over = _match_odds(extra_odds, ("total", "тотал", "set", "сет", "парт"), line=main_total, side="over")
    total_book_under = _match_odds(extra_odds, ("total", "тотал", "set", "сет", "парт"), line=main_total, side="under")
    if p_over_main >= 0.52:
        hints.append(
            _hint(
                kind="total_sets",
                market=f"ТБ {main_total} {unit}",
                side="over",
                line=main_total,
                model_prob=p_over_main,
                why=f"матч чаще идёт в {max_sets} {unit}" if p_full >= p_sweep else "часто не всухую",
                book=total_book_over,
            )
        )
    elif (1.0 - p_over_main) >= 0.52:
        hints.append(
            _hint(
                kind="total_sets",
                market=f"ТМ {main_total} {unit}",
                side="under",
                line=main_total,
                model_prob=round(1.0 - p_over_main, 4),
                why="фаворит чаще закрывает матч раньше",
                book=total_book_under,
            )
        )

    hc_book = _match_odds(extra_odds, ("handicap", "фора"), line=-1.5, side=side_w)
    if p_cover_15 >= 0.42:
        hints.append(
            _hint(
                kind="handicap",
                market=f"Фора {favorite} -1.5 {unit}",
                side=side_w,
                line=-1.5,
                model_prob=p_cover_15,
                why="покрытие -1.5 = победа с разницей минимум в две единицы",
                book=hc_book,
            )
        )
    elif p_cover_15 < 0.38:
        opp_side = "w2" if pick_home else "w1"
        hc_plus = _match_odds(extra_odds, ("handicap", "фора"), line=1.5, side=opp_side)
        hints.append(
            _hint(
                kind="handicap",
                market=f"Фора {underdog} +1.5 {unit}",
                side=opp_side,
                line=1.5,
                model_prob=round(1.0 - p_cover_15, 4),
                why="фаворит не настолько доминирует, чтобы стабильно -1.5",
                book=hc_plus,
            )
        )

    if abs(p_set - 0.5) >= 0.06:
        fs_book = _match_odds(
            extra_odds,
            ("first", "1st", "перв", "set_winner", "game_1"),
            side=side_w,
        )
        label = "первую партию" if sport == "table_tennis" else "первый сет"
        hints.append(
            _hint(
                kind="first_unit",
                market=f"{favorite} выиграет {label}",
                side=side_w,
                model_prob=p_set,
                why="P(сета/партии) из той же модели, что и победа в матче",
                book=fs_book,
            )
        )

    p_odd = 0.0
    p_even = 0.0
    for lost in range(need):
        sets = need + lost
        pr = dist.get(f"{need}-{lost}", 0.0) + dist.get(f"{lost}-{need}", 0.0)
        if sets % 2:
            p_odd += pr
        else:
            p_even += pr
    if max(p_odd, p_even) >= 0.58:
        odd = p_odd >= p_even
        oe_book = _match_odds(extra_odds, ("odd", "even", "чет", "нечет", "нечёт"), side="odd" if odd else "even")
        hints.append(
            _hint(
                kind="odd_even",
                market=f"{'Нечёт' if odd else 'Чёт'} {unit}",
                side="odd" if odd else "even",
                model_prob=p_odd if odd else p_even,
                why="нечёт = максимальная длительность, чёт = короче",
                book=oe_book,
            )
        )

    games = [row for row in extra_odds if ("game" in _blob(row) or "гейм" in _blob(row) or "очк" in _blob(row) or "point" in _blob(row))]
    if games:
        prefer_over = abs(p_match - 0.5) < 0.08
        prefer_under = p_match >= 0.70
        if prefer_over or prefer_under:
            side = "over" if prefer_over else "under"
            book = _match_odds(games, ("total", "тотал", "game", "гейм", "очк"), side=side)
            if book:
                hints.append(
                    _hint(
                        kind="total_games",
                        market=f"{book.get('market_name') or 'Тотал'}: {book.get('side_name') or side} {book.get('line')}",
                        side=side,
                        line=book.get("line"),
                        why="линия букмекера + оценка формата, без модели геймов",
                        book=book,
                        caution="не value: геймы/очки не считаются, только формат матча",
                    )
                )

    ranked = []
    for item in hints:
        prob = item.get("model_prob") or 0
        ranked.append((-(prob), item["kind"] != "exact_score", item))
    ranked.sort(key=lambda row: (row[0], row[1]))
    out = []
    seen = set()
    for _p, _k, item in ranked:
        key = str(item.get("market") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= 6:
            break
    return out


def merge_extra_bets(hints: list, ai_bets: list | None, extra_odds: list | None = None) -> list[dict]:
    extra_odds = extra_odds or []
    merged: list[dict] = []
    seen: set[str] = set()
    for item in list(hints or []) + list(ai_bets or []):
        if not isinstance(item, dict):
            continue
        market = str(item.get("market") or "").strip()[:90]
        if not market:
            continue
        key = market.lower()
        if key in seen:
            continue
        seen.add(key)
        odds = item.get("odds")
        try:
            odds = round(float(odds), 3) if odds is not None else None
        except (TypeError, ValueError):
            odds = None
        if odds is not None and extra_odds:
            allowed = {(str(row.get("side")), str(row.get("line"))) for row in extra_odds}
            odds_ok = any(abs(float(row.get("odds") or 0) - odds) < 0.011 for row in extra_odds)
            if (str(item.get("side") or ""), str(item.get("line"))) not in allowed or not odds_ok:
                odds = None
        model_prob = item.get("model_prob")
        try:
            model_prob = round(float(model_prob), 3) if model_prob is not None else None
        except (TypeError, ValueError):
            model_prob = None
        merged.append(
            {
                "kind": item.get("kind") or "extra",
                "market": market,
                "side": item.get("side"),
                "line": item.get("line"),
                "odds": odds,
                "bookmaker": item.get("bookmaker"),
                "model_prob": model_prob,
                "why": str(item.get("why") or item.get("reason") or "")[:220],
                "caution": str(item.get("caution") or "не value")[:160],
                "source": item.get("source") or "ai",
            }
        )
        if len(merged) >= 6:
            break
    return merged
