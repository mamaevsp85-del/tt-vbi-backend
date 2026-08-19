"""Исторический Elo из elo/data: tennis-data + ITF/Challenger (Sackmann)."""
from __future__ import annotations

import csv
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from app.core.config import ROOT_DIR

logger = logging.getLogger(__name__)

ELO_DIR = ROOT_DIR / "elo" / "data"
CSV_FILES = (
    ELO_DIR / "elo_players.csv",
    ELO_DIR / "elo_itf_players.csv",
)
MIN_HIST = 8


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().replace(".", " ").replace("-", " ").replace("'", " ")
    return re.sub(r"\s+", " ", text).strip()


def _surname_and_initial(name: str) -> tuple[str, str] | None:
    tokens = _fold(name).split()
    if len(tokens) < 2:
        return None
    if len(tokens[-1]) == 1:
        surname = tokens[-2]
        initial = tokens[-1]
        return surname, initial
    return tokens[-1], tokens[0][0]


def _load_csv(path: Path, by_name: dict[str, dict], by_key: dict[tuple[str, str, str], dict]) -> int:
    if not path.exists():
        return 0
    loaded = 0
    min_matches = 3 if path.name == "elo_itf_players.csv" else MIN_HIST
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("player") or "").strip()
            tour = (row.get("tour") or "").strip().upper()
            if not name or "/" in name:
                continue
            try:
                rec = {
                    "name": name,
                    "tour": tour,
                    "elo": float(row.get("elo") or 1500),
                    "matches": int(float(row.get("matches") or 0)),
                }
            except ValueError:
                continue
            if rec["matches"] < min_matches:
                continue
            folded = _fold(name)
            name_key = f"{tour}|{folded}"
            prev_name = by_name.get(name_key)
            if prev_name is None or rec["matches"] > prev_name["matches"]:
                by_name[name_key] = rec
            parsed = _surname_and_initial(name)
            if not parsed:
                continue
            key = (tour, parsed[0], parsed[1])
            prev = by_key.get(key)
            if prev is None or rec["matches"] > prev["matches"]:
                by_key[key] = rec
            loaded += 1
    return loaded


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, dict], dict[tuple[str, str, str], dict]]:
    by_name: dict[str, dict] = {}
    by_key: dict[tuple[str, str, str], dict] = {}
    total = 0
    for path in CSV_FILES:
        total += _load_csv(path, by_name, by_key)
    if total:
        logger.info("Исторический Elo: %s записей, %s имён, %s ключей", total, len(by_name), len(by_key))
    else:
        logger.warning("Нет CSV в %s — запустите elo/собрать.py или elo/import_sackmann.py", ELO_DIR)
    return by_name, by_key


def clear_cache() -> None:
    _index.cache_clear()


def lookup(player_name: str) -> dict | None:
    if not player_name or "/" in player_name:
        return None
    by_name, by_key = _index()
    folded = _fold(player_name)
    for tour in ("ATP", "WTA"):
        hit = by_name.get(f"{tour}|{folded}")
        if hit:
            return hit
    parsed = _surname_and_initial(player_name)
    if not parsed:
        return None
    hits = [by_key[key] for key in (("ATP",) + parsed, ("WTA",) + parsed) if key in by_key]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return max(hits, key=lambda item: item["matches"])
    return None


def blend(player_name: str, api_elo: float, api_matches: int) -> tuple[float, int]:
    hist = lookup(player_name)
    if hist and hist["matches"] >= max(MIN_HIST, api_matches):
        return hist["elo"], hist["matches"]
    if hist and hist["matches"] >= 3 and api_matches < 3:
        return hist["elo"], hist["matches"]
    return api_elo, api_matches
