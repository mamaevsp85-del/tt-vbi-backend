from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlayerElo(Base):
    __tablename__ = "player_elo"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sport: Mapped[str] = mapped_column(String(30), index=True)
    player_id: Mapped[str] = mapped_column(String(50), index=True)
    player_name: Mapped[str] = mapped_column(String(200), default="")
    elo: Mapped[float] = mapped_column(Float, default=1500)
    matches_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    sport: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(30), default="notstarted", index=True)
    tournament_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tournament_name: Mapped[str] = mapped_column(String(200), default="")
    player_a_id: Mapped[str] = mapped_column(String(50), default="")
    player_b_id: Mapped[str] = mapped_column(String(50), default="")
    player_a: Mapped[str] = mapped_column(String(200), default="")
    player_b: Mapped[str] = mapped_column(String(200), default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    winner: Mapped[str | None] = mapped_column(String(20), nullable=True)
    score_display: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MatchStat(Base):
    __tablename__ = "match_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(Integer, index=True)
    period: Mapped[str] = mapped_column(String(20), default="ALL")
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    serve_win_pct_a: Mapped[float | None] = mapped_column(Float, nullable=True)
    serve_win_pct_b: Mapped[float | None] = mapped_column(Float, nullable=True)
    receive_win_pct_a: Mapped[float | None] = mapped_column(Float, nullable=True)
    receive_win_pct_b: Mapped[float | None] = mapped_column(Float, nullable=True)
    points_a: Mapped[float | None] = mapped_column(Float, nullable=True)
    points_b: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    external_id: Mapped[str] = mapped_column(String(50), index=True)
    sport: Mapped[str] = mapped_column(String(30), index=True)
    sport_name: Mapped[str] = mapped_column(String(50), default="")
    tournament: Mapped[str] = mapped_column(String(200), default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    player_a: Mapped[str] = mapped_column(String(200), default="")
    player_b: Mapped[str] = mapped_column(String(200), default="")
    player_a_elo: Mapped[float] = mapped_column(Float, default=1500)
    player_b_elo: Mapped[float] = mapped_column(Float, default=1500)
    elo_matches_a: Mapped[int] = mapped_column(Integer, default=0)
    elo_matches_b: Mapped[int] = mapped_column(Integer, default=0)
    odds_a: Mapped[float | None] = mapped_column(Float, nullable=True)
    odds_b: Mapped[float | None] = mapped_column(Float, nullable=True)
    prob_a: Mapped[float] = mapped_column(Float, default=0.5)
    prob_b: Mapped[float] = mapped_column(Float, default=0.5)
    predicted_winner: Mapped[str] = mapped_column(String(200), default="")
    bookmaker: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_value: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    edge: Mapped[float] = mapped_column(Float, default=0)
    kelly_fraction: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    verdict: Mapped[str] = mapped_column(String(30), default="no_value")
    forecast_source: Mapped[str | None] = mapped_column(String(40), nullable=True)
    risk_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model_diagnostics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    h2h_home_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    h2h_away_wins: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_analysis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    settled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PredictionHistory(Base):
    __tablename__ = "prediction_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prediction_id: Mapped[int] = mapped_column(Integer, index=True)
    external_id: Mapped[str] = mapped_column(String(50), index=True)
    result: Mapped[str] = mapped_column(String(10), default="")
    actual_winner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    profit_loss: Mapped[float] = mapped_column(Float, default=0)
    roi: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
