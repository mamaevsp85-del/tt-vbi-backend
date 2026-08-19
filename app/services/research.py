from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

logger = logging.getLogger(__name__)

DDG_SEARCH_URL = "https://html.duckduckgo.com/html/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0 Safari/537.36"
)


def _clean_text(value: str) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    parsed = urlparse(raw_url)
    if "duckduckgo.com" in (parsed.netloc or ""):
        qs = parse_qs(parsed.query)
        uddg = qs.get("uddg")
        if uddg:
            return unquote(uddg[0])
    return raw_url


def _date_bits(prediction: dict[str, Any]) -> tuple[str, str]:
    raw = prediction.get("scheduled_at")
    if isinstance(raw, datetime):
        dt = raw
    elif raw:
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return "", ""
    else:
        return "", ""
    return dt.strftime("%Y-%m-%d"), str(dt.year)


def _normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _clean_search_variant(value: str) -> str:
    text = _normalize_spaces(value)
    text = re.sub(r"[\"'`]+", "", text)
    text = re.sub(r"[(){}\[\],:;]+", " ", text)
    text = re.sub(r"[./\\\-]+", " ", text)
    return _normalize_spaces(text)


def _player_variants(value: str) -> list[str]:
    original = _normalize_spaces(value)
    if not original:
        return []

    variants: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        text = _normalize_spaces(candidate)
        lowered = text.casefold()
        if text and lowered not in seen:
            seen.add(lowered)
            variants.append(text)

    add(original)
    add(_clean_search_variant(original))
    add(re.sub(r"[.'`]+", "", original))

    tokens = [token for token in re.split(r"\s+", re.sub(r"[\"'`]+", "", original)) if token]
    if len(tokens) >= 2:
        surname = tokens[0]
        given = tokens[1]
        initial_match = re.match(r"([A-Za-zА-Яа-яЁё])", given)
        if initial_match:
            initial = initial_match.group(1)
            add(f"{surname} {initial}.")
            add(f"{surname} {initial}")
    return variants


def _tournament_variants(value: str) -> list[str]:
    original = _normalize_spaces(value)
    if not original:
        return []

    variants: list[str] = []
    seen: set[str] = set()

    for candidate in [original, _clean_search_variant(original)]:
        lowered = candidate.casefold()
        if candidate and lowered not in seen:
            seen.add(lowered)
            variants.append(candidate)
    return variants


def _quoted_if_needed(value: str) -> str:
    text = _normalize_spaces(value)
    if not text:
        return ""
    return f'"{text}"' if " " in text else text


def _add_query(queries: list[str], seen: set[str], value: str, *, limit: int) -> None:
    text = _normalize_spaces(value)
    lowered = text.casefold()
    if text and lowered not in seen and len(queries) < limit:
        seen.add(lowered)
        queries.append(text)


def build_queries(prediction: dict[str, Any]) -> list[str]:
    player_a = str(prediction.get("player_a") or "").strip()
    player_b = str(prediction.get("player_b") or "").strip()
    tournament = str(prediction.get("tournament") or "").strip()
    sport_name = str(prediction.get("sport_name") or prediction.get("sport") or "").strip()
    iso_day, year = _date_bits(prediction)

    player_a_variants = _player_variants(player_a)
    player_b_variants = _player_variants(player_b)
    tournament_variants = _tournament_variants(tournament)
    sport_variant = _normalize_spaces(sport_name)

    primary_a = player_a_variants[0] if player_a_variants else ""
    primary_b = player_b_variants[0] if player_b_variants else ""
    compact_a = player_a_variants[1] if len(player_a_variants) > 1 else primary_a
    compact_b = player_b_variants[1] if len(player_b_variants) > 1 else primary_b
    short_a = player_a_variants[-1] if player_a_variants else ""
    short_b = player_b_variants[-1] if player_b_variants else ""
    primary_tournament = tournament_variants[0] if tournament_variants else ""
    clean_tournament = tournament_variants[1] if len(tournament_variants) > 1 else primary_tournament

    keywords = ["result", "score", "live score", "winner"]
    dated_keywords = ["result", "score"]
    query_limit = 14
    queries: list[str] = []
    seen: set[str] = set()

    player_pairs = [
        (primary_a, primary_b),
        (compact_a, compact_b),
        (short_a, short_b),
    ]

    # Prioritize FlashscoreKZ results for tennis to improve settlement hit-rate.
    # Keep this block first so `web_facts(... max_queries=3)` tends to pick FlashscoreKZ links.
    flashscore_site_variants = [
        "site:flashscorekz.com/tennis",
        'site:flashscorekz.com "tennis"',
    ]
    flashscore_keywords = ["live", "live score", "result", "score", "winner"]
    flashscore_limit = max(6, query_limit // 2)

    for site_filter in flashscore_site_variants:
        for player_x, player_y in player_pairs:
            if not player_x or not player_y:
                continue
            quoted_x = _quoted_if_needed(player_x)
            quoted_y = _quoted_if_needed(player_y)
            for keyword in flashscore_keywords:
                _add_query(
                    queries,
                    seen,
                    " ".join(part for part in [site_filter, quoted_x, quoted_y, sport_variant, keyword] if part),
                    limit=flashscore_limit,
                )

    for player_x, player_y in player_pairs:
        if not player_x or not player_y:
            continue
        quoted_x = _quoted_if_needed(player_x)
        quoted_y = _quoted_if_needed(player_y)
        for keyword in keywords:
            _add_query(
                queries,
                seen,
                " ".join(part for part in [quoted_x, quoted_y, sport_variant, keyword] if part),
                limit=query_limit,
            )
            if primary_tournament:
                _add_query(
                    queries,
                    seen,
                    " ".join(part for part in [quoted_x, quoted_y, primary_tournament, keyword] if part),
                    limit=query_limit,
                )
            if clean_tournament and clean_tournament != primary_tournament:
                _add_query(
                    queries,
                    seen,
                    " ".join(part for part in [quoted_x, quoted_y, clean_tournament, keyword] if part),
                    limit=query_limit,
                )

    if primary_a and primary_b:
        base_parts = [primary_a, primary_b, primary_tournament or clean_tournament, sport_variant, year]
        for keyword in keywords:
            _add_query(
                queries,
                seen,
                " ".join(part for part in [*base_parts, keyword] if part),
                limit=query_limit,
            )

    if iso_day and primary_a and primary_b:
        quoted_a = _quoted_if_needed(primary_a)
        quoted_b = _quoted_if_needed(primary_b)
        for keyword in dated_keywords:
            _add_query(
                queries,
                seen,
                " ".join(part for part in [quoted_a, quoted_b, iso_day, keyword] if part),
                limit=query_limit,
            )
            if primary_tournament:
                _add_query(
                    queries,
                    seen,
                    " ".join(part for part in [quoted_a, quoted_b, primary_tournament, iso_day, keyword] if part),
                    limit=query_limit,
                )

    return queries


async def duckduckgo_search(query: str, *, max_results: int = 5) -> list[dict[str, str]]:
    attempts = 3
    timeout_seconds = 35.0

    body: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                headers={"User-Agent": USER_AGENT},
                trust_env=True,
            ) as client:
                response = await client.post(DDG_SEARCH_URL, data={"q": query})
                response.raise_for_status()
            body = response.text
            break
        except (httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
            logger.warning(
                "DuckDuckGo search timeout (attempt %s/%s) for query=%s: %s",
                attempt,
                attempts,
                query,
                exc,
            )
            if attempt < attempts:
                backoff_s = 2 ** (attempt - 1)  # 1s, 2s, ...
                await asyncio.sleep(backoff_s)
        except Exception:
            logger.exception("DuckDuckGo search failed for query=%s", query)

    if body is None:
        return []

    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'(?:<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>|<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>)'
        r'(?P<snippet>.*?)</(?:a|div)>',
        re.DOTALL | re.IGNORECASE,
    )
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in pattern.finditer(body):
        url = _extract_url(match.group("href"))
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": _clean_text(match.group("title")),
                "url": url,
                "snippet": _clean_text(match.group("snippet")),
            }
        )
        if len(results) >= max_results:
            break
    if not results and isinstance(body, str):
        try:
            fallback_pattern = re.compile(
                r"""href\s*=\s*(?P<quote>["']?)(?P<href>(?:(?!\1|\s|>).)*?(?:/l/\?uddg=|uddg=)(?:(?!\1|\s|>).)*)(?P=quote)""",
                re.IGNORECASE,
            )
            for match in fallback_pattern.finditer(body):
                href = html.unescape(match.group("href") or "")
                url = _extract_url(href)
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append({"title": "", "url": url, "snippet": ""})
                if len(results) >= max_results:
                    break
        except Exception:
            logger.debug("DuckDuckGo fallback parse failed for query=%s", query, exc_info=True)
    return results


async def fetch_page_text(url: str, *, max_chars: int = 2500) -> str:
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
            trust_env=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except Exception:
        logger.debug("Page fetch failed for %s", url, exc_info=True)
        return ""

    text = response.text
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = _clean_text(text)
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


def dossier(prediction: dict[str, Any]) -> str:
    return (
        "МАТЧ\n"
        f"- sport: {prediction.get('sport_name') or prediction.get('sport')}\n"
        f"- tournament: {prediction.get('tournament') or ''}\n"
        f"- scheduled_at: {prediction.get('scheduled_at') or ''}\n"
        f"- player_a: {prediction.get('player_a') or ''}\n"
        f"- player_b: {prediction.get('player_b') or ''}\n"
    )


async def web_facts(prediction: dict[str, Any], *, max_queries: int = 3, max_pages: int = 3) -> dict[str, Any]:
    queries = build_queries(prediction)[:max_queries]
    search_results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for query in queries:
        for item in await duckduckgo_search(query, max_results=max_pages):
            url = item.get("url") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            search_results.append({**item, "query": query})
            if len(search_results) >= max_pages:
                break
        if len(search_results) >= max_pages:
            break

    pages: list[dict[str, str]] = []
    for item in search_results[:max_pages]:
        excerpt = await fetch_page_text(item["url"])
        pages.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("snippet") or "",
                "query": item.get("query") or "",
                "excerpt": excerpt,
            }
        )
    return {"queries": queries, "results": search_results, "pages": pages}


async def web_brief(prediction: dict[str, Any], *, max_queries: int = 3, max_pages: int = 3) -> str:
    facts = await web_facts(prediction, max_queries=max_queries, max_pages=max_pages)
    if not facts["pages"]:
        return "ФАКТЫ ИЗ ИНТЕРНЕТА\n- Ничего надежного не найдено."

    lines = ["ФАКТЫ ИЗ ИНТЕРНЕТА"]
    for idx, item in enumerate(facts["pages"], start=1):
        lines.append(f"{idx}. {item['title']}")
        lines.append(f"   url: {item['url']}")
        if item.get("snippet"):
            lines.append(f"   snippet: {item['snippet']}")
        if item.get("excerpt"):
            lines.append(f"   excerpt: {item['excerpt']}")
    return "\n".join(lines)
