from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services import quota

logger = logging.getLogger(__name__)

SPORT_SLUGS = {
    "table_tennis": "table-tennis",
    "tennis": "tennis",
}


class APISportError(RuntimeError):
    pass


class APISportClient:
    def __init__(self) -> None:
        self.base_url = settings.api_sport_base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        key = quota.active_key()
        if not key:
            raise APISportError("Не задан API_SPORT_KEY в .env")
        return {"Authorization": key}

    async def get_matches(
        self,
        sport: str,
        *,
        status: str | None = "notstarted",
        page: int = 1,
        page_size: int | None = None,
        with_pregame: bool = True,
        with_bk_odds: bool = True,
        has_bk_odds: bool | None = None,
        bookmaker_ids: str | None = None,
        team_id: str | None = None,
        tournament_id: str | None = None,
        sort: str | None = "asc",
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        client: httpx.AsyncClient | None = None,
        skip_quota: bool = False,
    ) -> list[dict[str, Any]]:
        matches, _total = await self.get_matches_page(
            sport,
            status=status,
            page=page,
            page_size=page_size,
            with_pregame=with_pregame,
            with_bk_odds=with_bk_odds,
            has_bk_odds=has_bk_odds,
            bookmaker_ids=bookmaker_ids,
            team_id=team_id,
            tournament_id=tournament_id,
            sort=sort,
            date=date,
            date_from=date_from,
            date_to=date_to,
            client=client,
            skip_quota=skip_quota,
        )
        return matches

    async def get_matches_page(
        self,
        sport: str,
        *,
        status: str | None = "notstarted",
        page: int = 1,
        page_size: int | None = None,
        with_pregame: bool = True,
        with_bk_odds: bool = True,
        has_bk_odds: bool | None = None,
        bookmaker_ids: str | None = None,
        team_id: str | None = None,
        tournament_id: str | None = None,
        sort: str | None = "asc",
        date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        client: httpx.AsyncClient | None = None,
        skip_quota: bool = False,
    ) -> tuple[list[dict[str, Any]], int | None]:
        slug = SPORT_SLUGS.get(sport)
        if not slug:
            raise APISportError(f"Неизвестный вид спорта: {sport}")

        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size if page_size is not None else settings.api_sport_page_size,
        }
        if status:
            params["status"] = status
        if with_pregame:
            params["with_pregame"] = "true"
        if with_bk_odds:
            params["with_bk_odds"] = "true"
        if has_bk_odds is True:
            params["has_bk_odds"] = "true"
        if bookmaker_ids:
            params["bookmaker_ids"] = bookmaker_ids
        if team_id:
            params["team_id"] = team_id
        if tournament_id:
            params["tournament_id"] = tournament_id
        if sort:
            params["sort"] = sort
        if date:
            params["date"] = date
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to

        url = f"{self.base_url}/v2/{slug}/matches"

        async def _do(http: httpx.AsyncClient) -> tuple[list[dict[str, Any]], int | None]:
            if not skip_quota:
                quota.acquire(1)
            response = await http.get(url, params=params, headers=self._headers())
            if response.status_code == 429:
                rotated = quota.mark_exhausted()
                if rotated:
                    logger.warning("API-Sport %s 429: повтор на запасном ключе", sport)
                    if not skip_quota:
                        quota.acquire(1)
                    response = await http.get(url, params=params, headers=self._headers())
                if response.status_code == 429:
                    quota.mark_exhausted()
                    logger.error("API-Sport %s 429: все ключи упёрлись в дневной лимит", sport)
                    raise APISportError("API-Sport вернул 429")
            if response.status_code != 200:
                logger.error("API-Sport %s %s: %s", sport, response.status_code, response.text[:300])
                raise APISportError(f"API-Sport вернул {response.status_code}")
            payload = response.json()
            matches = payload.get("matches")
            if matches is None:
                matches = payload.get("data") or []
            total = payload.get("totalMatches") or payload.get("total")
            if total is not None:
                try:
                    total = int(total)
                except (TypeError, ValueError):
                    total = None
            snap = quota.snapshot()
            logger.info(
                "API-Sport %s status=%s page=%s team=%s: %s/%s матчей (квота %s/%s)",
                sport,
                status,
                page,
                team_id or "-",
                len(matches),
                total if total is not None else "?",
                snap["used"],
                snap["budget"],
            )
            return matches, total

        if client is not None:
            return await _do(client)
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as http:
            return await _do(http)

    async def get_match_by_id(
        self,
        sport: str,
        match_id: str | int,
        *,
        with_pregame: bool = True,
        with_bk_odds: bool = False,
        client: httpx.AsyncClient | None = None,
        skip_quota: bool = False,
    ) -> dict[str, Any] | None:
        """
        Запрос конкретного матча по ID:
        GET /v2/{sportSlug}/matches/{matchId}
        """
        slug = SPORT_SLUGS.get(sport)
        if not slug:
            raise APISportError(f"Неизвестный вид спорта: {sport}")

        url = f"{self.base_url}/v2/{slug}/matches/{match_id}"
        params: dict[str, Any] = {}
        if with_pregame:
            params["with_pregame"] = "true"
        if with_bk_odds:
            params["with_bk_odds"] = "true"

        async def _do(http: httpx.AsyncClient) -> dict[str, Any] | None:
            if not skip_quota:
                quota.acquire(1)
            response = await http.get(url, params=params, headers=self._headers())
            if response.status_code == 404:
                return None
            if response.status_code == 429:
                rotated = quota.mark_exhausted()
                if rotated:
                    if not skip_quota:
                        quota.acquire(1)
                    response = await http.get(url, params=params, headers=self._headers())
                if response.status_code == 429:
                    quota.mark_exhausted()
                    raise APISportError("API-Sport вернул 429")
            if response.status_code != 200:
                raise APISportError(f"API-Sport вернул {response.status_code}")
            payload = response.json()
            # API иногда отдаёт объект матча напрямую, иногда в ключе data/match
            if isinstance(payload, dict) and "match" in payload:
                return payload.get("match")  # type: ignore[return-value]
            if isinstance(payload, dict) and "data" in payload:
                return payload.get("data")  # type: ignore[return-value]
            return payload

        if client is not None:
            return await _do(client)
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as http:
            return await _do(http)

    async def fetch_line(
        self,
        sport: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        bookmaker_ids: str = "melbet,betboom,marathon,pari",
        include_live: bool | None = None,
        max_pages: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Все страницы прематч + live с кэфами БК (с учётом квоты)."""
        page_size = int(settings.api_sport_page_size)
        page_limit = max(1, int(max_pages or settings.api_sport_max_pages))
        live = settings.fetch_live_matches if include_live is None else include_live
        statuses = ["notstarted"] + (["inprogress"] if live else [])
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        stats = {"api_total": 0, "pages": 0, "live": 0, "prematch": 0}

        async def _fetch_status(http: httpx.AsyncClient, status: str) -> None:
            page = 1
            total: int | None = None
            while page <= page_limit:
                try:
                    chunk, total = await self.get_matches_page(
                        sport,
                        status=status,
                        page=page,
                        page_size=page_size,
                        with_pregame=True,
                        with_bk_odds=True,
                        has_bk_odds=True,
                        bookmaker_ids=bookmaker_ids,
                        sort="asc",
                        date_from=date_from,
                        date_to=date_to,
                        client=http,
                    )
                except quota.QuotaError:
                    logger.warning("fetch_line %s %s: квота на странице %s", sport, status, page)
                    break
                stats["pages"] += 1
                if page == 1 and total is not None:
                    stats["api_total"] += total
                if not chunk:
                    break
                for raw in chunk:
                    ext = str(raw.get("id") or "")
                    if not ext or ext in seen:
                        continue
                    seen.add(ext)
                    out.append(raw)
                    if status == "inprogress":
                        stats["live"] += 1
                    else:
                        stats["prematch"] += 1
                if len(chunk) < page_size:
                    break
                if total is not None and page * page_size >= total:
                    break
                page += 1

        if client is not None:
            for status in statuses:
                await _fetch_status(client, status)
        else:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as http:
                for status in statuses:
                    await _fetch_status(http, status)
        return out, stats

    async def player_histories(
        self,
        sport: str,
        player_ids: list[str],
        page_size: int = 25,
        *,
        skip_quota: bool = False,
    ) -> list[dict[str, Any]]:
        unique = []
        seen: set[str] = set()
        for pid in player_ids:
            if pid and pid not in seen:
                seen.add(pid)
                unique.append(pid)
        unique = unique[: max(0, int(settings.api_sport_history_players_per_refresh))]
        if not unique:
            return []

        out: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30.0) as http:
            for pid in unique:
                try:
                    chunk = await self.get_matches(
                        sport,
                        status="finished",
                        page=1,
                        page_size=page_size,
                        with_pregame=False,
                        with_bk_odds=False,
                        has_bk_odds=None,
                        team_id=pid,
                        client=http,
                        skip_quota=skip_quota,
                    )
                    out.extend(chunk)
                except quota.QuotaError:
                    logger.warning("История игроков остановлена: дневная квота")
                    break
                except Exception:
                    logger.exception("История игрока %s не загрузилась", pid)
        return out
