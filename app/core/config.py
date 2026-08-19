from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TT-VBI"
    debug: bool = True
    log_level: str = "INFO"

    api_sport_key: str = ""
    api_sport_key_backup: str = ""
    api_sport_base_url: str = "https://api.api-sport.ru"
    deepseek_api_key: str = ""

    database_url: str = "sqlite:///data/tt_vbi.db"

    min_odds_threshold: float = 1.40
    min_edge_threshold: float = 0.03
    max_daily_signals: int = 5
    fetch_player_history: bool = False
    api_sport_daily_budget: int = 12
    api_sport_refresh_cooldown_sec: int = 900
    api_sport_history_players_per_refresh: int = 8
    api_sport_page_size: int = 100
    api_sport_max_pages: int = 5
    fetch_live_matches: bool = True

    backfill_days_default: int = 30
    backfill_page_size: int = 50
    backfill_respect_quota: bool = True
    backfill_players_limit: int = 15
    sackmann_mirror_url: str = "https://huggingface.co/datasets/Aneeshers/tennis-sackmann-archive/resolve/main"
    deepseek_thin_cap: int = 12
    deepseek_web_research: bool = True

    cors_origins: str = "http://localhost:8080,http://127.0.0.1:8080"
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    timezone: str = "Asia/Krasnoyarsk"

    @property
    def cors_origin_list(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


settings = Settings()
