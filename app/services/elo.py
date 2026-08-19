from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.models import PlayerElo, utcnow

DEFAULT_ELO = 1500.0
K_FACTOR = 32.0


def get_or_create_player(db: Session, sport: str, player_id: str, name: str) -> PlayerElo:
    row = (
        db.query(PlayerElo)
        .filter(PlayerElo.sport == sport, PlayerElo.player_id == player_id)
        .one_or_none()
    )
    if row:
        if name and row.player_name != name:
            row.player_name = name
        return row
    row = PlayerElo(
        sport=sport,
        player_id=player_id or name,
        player_name=name,
        elo=DEFAULT_ELO,
        matches_count=0,
    )
    db.add(row)
    db.flush()
    return row


def expected_score(elo_a: float, elo_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((elo_b - elo_a) / 400.0))


def update_from_finished(
    db: Session,
    *,
    sport: str,
    player_a_id: str,
    player_a_name: str,
    player_b_id: str,
    player_b_name: str,
    winner: str | None,
) -> None:
    if winner not in {"home", "away"}:
        return
    if not player_a_id or not player_b_id:
        return

    a = get_or_create_player(db, sport, player_a_id, player_a_name)
    b = get_or_create_player(db, sport, player_b_id, player_b_name)
    result_a = 1.0 if winner == "home" else 0.0
    exp_a = expected_score(a.elo, b.elo)
    a.elo = round(a.elo + K_FACTOR * (result_a - exp_a), 2)
    b.elo = round(b.elo + K_FACTOR * ((1.0 - result_a) - (1.0 - exp_a)), 2)
    a.matches_count += 1
    b.matches_count += 1
    a.updated_at = utcnow()
    b.updated_at = utcnow()


def ratings(db: Session, sport: str, player_a_id: str, player_a_name: str, player_b_id: str, player_b_name: str) -> tuple[float, float, int, int]:
    a = get_or_create_player(db, sport, player_a_id or player_a_name, player_a_name)
    b = get_or_create_player(db, sport, player_b_id or player_b_name, player_b_name)
    elo_a, n_a = a.elo, a.matches_count
    elo_b, n_b = b.elo, b.matches_count
    if sport == "tennis":
        from app.services.historical_elo import blend

        elo_a, n_a = blend(player_a_name, elo_a, n_a)
        elo_b, n_b = blend(player_b_name, elo_b, n_b)
    return elo_a, elo_b, n_a, n_b


def rebuild_sport_elo(db: Session, sport: str) -> int:
    from app.core.models import Match

    db.query(PlayerElo).filter(PlayerElo.sport == sport).delete(synchronize_session=False)
    rows = (
        db.query(Match)
        .filter(
            Match.sport == sport,
            Match.status == "finished",
            Match.winner.in_(("home", "away")),
        )
        .order_by(Match.scheduled_at.asc(), Match.id.asc())
        .all()
    )
    for row in rows:
        update_from_finished(
            db,
            sport=sport,
            player_a_id=row.player_a_id,
            player_a_name=row.player_a,
            player_b_id=row.player_b_id,
            player_b_name=row.player_b,
            winner=row.winner,
        )
    db.flush()
    return len(rows)


def recent_winrate(db: Session, sport: str, player_id: str, limit: int = 6) -> float | None:
    from app.core.models import Match

    if not player_id:
        return None
    rows = (
        db.query(Match)
        .filter(
            Match.sport == sport,
            Match.status == "finished",
            Match.winner.in_(("home", "away")),
            (Match.player_a_id == player_id) | (Match.player_b_id == player_id),
        )
        .order_by(Match.scheduled_at.desc())
        .limit(limit)
        .all()
    )
    if len(rows) < 3:
        return None
    wins = 0
    for row in rows:
        if row.player_a_id == player_id and row.winner == "home":
            wins += 1
        elif row.player_b_id == player_id and row.winner == "away":
            wins += 1
    return wins / len(rows)
