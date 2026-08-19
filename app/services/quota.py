from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import ROOT_DIR, settings

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PATH = ROOT_DIR / "data" / "api_quota.json"


class QuotaError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 429, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload or {}


def _today() -> str:
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date().isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict[str, Any]:
    today = _today()
    blank = {
        "date": today,
        "used": 0,
        "exhausted": False,
        "last_refresh": {},
        "key_index": 0,
    }
    if not _PATH.exists():
        return blank
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return blank
    if data.get("date") != today:
        return blank
    data.setdefault("used", 0)
    data.setdefault("exhausted", False)
    data.setdefault("last_refresh", {})
    data.setdefault("key_index", 0)
    return data


def _save(data: dict[str, Any]) -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def key_list() -> list[str]:
    env_values: dict[str, str] = {}
    env_path = ROOT_DIR / ".env"
    if env_path.exists():
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                clean_key = key.strip().lstrip("\ufeff")
                env_values[clean_key] = value.strip().strip('"').strip("'")
        except Exception:
            logger.debug("Failed to read .env keys for quota", exc_info=True)

    out: list[str] = []
    def _add(raw: str | None) -> None:
        key = (raw or "").strip()
        if key and key not in out:
            out.append(key)

    # Основные ключи (legacy): API_SPORT_KEY + API_SPORT_KEY_BACKUP.
    _add(env_values.get("API_SPORT_KEY") or settings.api_sport_key)
    _add(env_values.get("API_SPORT_KEY_BACKUP") or settings.api_sport_key_backup)

    # Доп. ключи для ротации: API_SPORT_KEY_2..API_SPORT_KEY_5
    # (достаточно для “добавь еще один ключ” без полноценной миграции схемы).
    for i in range(2, 6):
        _add(env_values.get(f"API_SPORT_KEY_{i}"))
    return out


def _rotate_unlocked(data: dict[str, Any]) -> bool:
    keys = key_list()
    idx = int(data.get("key_index") or 0)
    if idx + 1 >= len(keys):
        data["exhausted"] = True
        return False
    data["key_index"] = idx + 1
    data["used"] = 0
    data["exhausted"] = False
    data["rotated_at"] = _now().isoformat()
    logger.info("API-Sport: ключ %s исчерпан, переключение на %s/%s", idx + 1, idx + 2, len(keys))
    return True


def rotate_invalid_key() -> bool:
    """Skip key that returned 401 Unauthorized and try the next one."""
    with _LOCK:
        data = _load()
        keys = key_list()
        idx = int(data.get("key_index") or 0)
        if idx + 1 >= len(keys):
            data["exhausted"] = True
            _save(data)
            return False
        data["key_index"] = idx + 1
        data["used"] = 0
        data["exhausted"] = False
        data["rotated_at"] = _now().isoformat()
        _save(data)
        logger.warning("API-Sport: ключ %s недействителен (401), пробуем %s/%s", idx + 1, idx + 2, len(keys))
        return True


def active_key() -> str:
    keys = key_list()
    if not keys:
        return ""
    with _LOCK:
        idx = int(_load().get("key_index") or 0)
    return keys[min(idx, len(keys) - 1)]


def snapshot() -> dict[str, Any]:
    with _LOCK:
        data = _load()
        budget = max(1, int(settings.api_sport_daily_budget))
        used = int(data.get("used") or 0)
        remaining = max(0, budget - used)
        if data.get("exhausted"):
            remaining = 0
        return {
            "date": data["date"],
            "used": used,
            "budget": budget,
            "remaining": remaining,
            "exhausted": bool(data.get("exhausted")),
            "cooldown_sec": int(settings.api_sport_refresh_cooldown_sec),
            "last_refresh": data.get("last_refresh") or {},
            "history_fetch": bool(settings.fetch_player_history),
            "key_slot": int(data.get("key_index") or 0) + 1,
            "key_total": max(1, len(key_list())),
            "has_backup": len(key_list()) > 1,
        }


def acquire(n: int = 1) -> None:
    with _LOCK:
        data = _load()
        budget = max(1, int(settings.api_sport_daily_budget))
        used = int(data.get("used") or 0)
        if data.get("exhausted") or used + n > budget:
            if _rotate_unlocked(data):
                used = 0
            else:
                raise QuotaError(
                    f"Дневной лимит API-Sport: {used}/{budget}. Обновление остановлено.",
                    status_code=429,
                    payload=snapshot_unlocked(data),
                )
        data["used"] = used + n
        _save(data)


def refund(n: int = 1) -> None:
    with _LOCK:
        data = _load()
        data["used"] = max(0, int(data.get("used") or 0) - n)
        _save(data)


def mark_exhausted() -> bool:
    with _LOCK:
        data = _load()
        rotated = _rotate_unlocked(data)
        if not rotated:
            data["exhausted"] = True
        _save(data)
        return rotated


def check_refresh(sport: str) -> None:
    with _LOCK:
        data = _load()
        budget = max(1, int(settings.api_sport_daily_budget))
        used = int(data.get("used") or 0)
        if data.get("exhausted") or used >= budget:
            if _rotate_unlocked(data):
                _save(data)
                used = 0
            else:
                raise QuotaError(
                    f"Дневной лимит API-Sport исчерпан ({used}/{budget}). Запасной ключ не задан или тоже кончился.",
                    status_code=429,
                    payload=snapshot_unlocked(data),
                )
        raw = (data.get("last_refresh") or {}).get(sport)
        cooldown = int(settings.api_sport_refresh_cooldown_sec)
        if raw and cooldown > 0:
            try:
                last = datetime.fromisoformat(raw)
            except ValueError:
                last = None
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                wait = cooldown - int((_now() - last).total_seconds())
                if wait > 0:
                    raise QuotaError(
                        f"Пауза {wait} сек. до следующего запроса {sport}. Кэш уже на сайте.",
                        status_code=429,
                        payload={**snapshot_unlocked(data), "retry_after_sec": wait},
                    )


def mark_refresh(sport: str) -> None:
    with _LOCK:
        data = _load()
        last = dict(data.get("last_refresh") or {})
        last[sport] = _now().isoformat()
        data["last_refresh"] = last
        _save(data)


def snapshot_unlocked(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or _load()
    budget = max(1, int(settings.api_sport_daily_budget))
    used = int(data.get("used") or 0)
    remaining = 0 if data.get("exhausted") else max(0, budget - used)
    keys = key_list()
    return {
        "date": data.get("date"),
        "used": used,
        "budget": budget,
        "remaining": remaining,
        "exhausted": bool(data.get("exhausted")),
        "cooldown_sec": int(settings.api_sport_refresh_cooldown_sec),
        "last_refresh": data.get("last_refresh") or {},
        "history_fetch": bool(settings.fetch_player_history),
        "key_slot": int(data.get("key_index") or 0) + 1,
        "key_total": max(1, len(keys)),
        "has_backup": len(keys) > 1,
    }
