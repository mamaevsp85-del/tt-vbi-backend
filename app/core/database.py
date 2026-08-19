from __future__ import annotations

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

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


def _configure_sqlite_connection(dbapi_conn, _connection_record) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=60000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


_db_url = _sqlite_url()
_is_sqlite = _db_url.startswith("sqlite")
_engine_kwargs: dict = {"echo": False, "future": True}
if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 60}
    _engine_kwargs["poolclass"] = NullPool

engine = create_engine(_db_url, **_engine_kwargs)
if _is_sqlite:
    event.listen(engine, "connect", _configure_sqlite_connection)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def release_db_lock(db: Session) -> None:
    """Commit to end the SQLite transaction and release write locks."""
    db.commit()


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
