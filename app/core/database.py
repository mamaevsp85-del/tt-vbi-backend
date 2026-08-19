from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import ROOT_DIR, settings


class Base(DeclarativeBase):
    pass


def _sqlite_url() -> str:
    url = settings.database_url
    if url.startswith("sqlite:///"):
        relative = url.replace("sqlite:///", "", 1)
        db_path = (ROOT_DIR / relative).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path.as_posix()}"
    return url


engine = create_engine(
    _sqlite_url(),
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app.core import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


def _migrate_sqlite() -> None:
    if not str(engine.url).startswith("sqlite"):
        return
    statements = {
        "elo_matches_a": "ALTER TABLE predictions ADD COLUMN elo_matches_a INTEGER DEFAULT 0",
        "elo_matches_b": "ALTER TABLE predictions ADD COLUMN elo_matches_b INTEGER DEFAULT 0",
        "forecast_source": "ALTER TABLE predictions ADD COLUMN forecast_source VARCHAR(40)",
        "risk_tier": "ALTER TABLE predictions ADD COLUMN risk_tier VARCHAR(20)",
        "model_diagnostics": "ALTER TABLE predictions ADD COLUMN model_diagnostics JSON",
    }
    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(predictions)"))}
        for name, sql in statements.items():
            if name not in cols:
                conn.execute(text(sql))
