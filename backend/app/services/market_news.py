from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

from defusedxml import ElementTree
import httpx

logger = logging.getLogger(__name__)

MARKET_NEWS_LIMIT = 6
MARKET_NEWS_CACHE_TTL = timedelta(minutes=30)
MARKET_NEWS_EMPTY_CACHE_TTL = timedelta(seconds=45)
MARKET_NEWS_REQUEST_TIMEOUT = 15.0

OFFICIAL_MARKET_NEWS_FEEDS = (
    ("Bank of Canada", "bank_of_canada", "https://www.bankofcanada.ca/utility/news/feed/"),
    ("Bank of Canada", "bank_of_canada", "https://www.bankofcanada.ca/content_type/summary-of-deliberations/feed/"),
    ("Federal Reserve", "federal_reserve", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("Federal Reserve", "federal_reserve", "https://www.federalreserve.gov/feeds/speeches_and_testimony.xml"),
    ("Bureau of Labor Statistics", "bls", "https://www.bls.gov/feed/cpi.rss"),
    ("Bureau of Labor Statistics", "bls", "https://www.bls.gov/feed/empsit.rss"),
    ("Bureau of Labor Statistics", "bls", "https://www.bls.gov/feed/ppi.rss"),
    ("Bureau of Labor Statistics", "bls", "https://www.bls.gov/feed/jolts.rss"),
    ("Bureau of Economic Analysis", "bea", "https://apps.bea.gov/rss/rss.xml"),
    ("U.S. Energy Information Administration", "eia", "https://www.eia.gov/rss/todayinenergy.xml"),
    ("Securities and Exchange Commission", "sec", "https://www.sec.gov/news/pressreleases.rss"),
)

MARKET_RELEVANCE_TERMS = (
    "ai",
    "bank of canada",
    "banks",
    "bitcoin",
    "bond",
    "bonds",
    "canadian dollar",
    "commodity",
    "commodities",
    "crude",
    "dollar",
    "dow",
    "earnings",
    "energy",
    "fed",
    "federal reserve",
    "gdp",
    "gold",
    "hormuz",
    "inflation",
    "interest rate",
    "iran",
    "jobs",
    "loonie",
    "market",
    "markets",
    "middle east",
    "nasdaq",
    "opec",
    "oil",
    "red sea",
    "recession",
    "s&p",
    "sanctions",
    "shipping",
    "stocks",
    "supply chain",
    "taiwan",
    "trade",
    "tariff",
    "tariffs",
    "treasury",
    "tsx",
    "wall street",
    "war",
)

LOW_SIGNAL_TITLE_TERMS = (
    "anniversary",
    "exhibition",
    "museum",
)


@dataclass(frozen=True)
class MarketNewsCandidate:
    title: str
    url: str
    source: str
    published_at: datetime
    image_url: str | None
    source_kind: str
    score: int


_market_news_cache: tuple[datetime, dict] | None = None
_market_news_lock = asyncio.Lock()


def _cache_ttl_for_payload(payload: dict) -> timedelta:
    articles = payload.get("articles") if isinstance(payload, dict) else []
    if isinstance(articles, list) and articles:
        return MARKET_NEWS_CACHE_TTL
    return MARKET_NEWS_EMPTY_CACHE_TTL


def _clean_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())


def _parse_feed_date(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _find_feed_text(element: Any, *names: str) -> str:
    for child in list(element):
        local_name = str(child.tag).rsplit("}", 1)[-1]
        if local_name in names:
            return "".join(child.itertext()).strip()
    return ""


def _find_feed_link(element: Any) -> str:
    for child in list(element):
        local_name = str(child.tag).rsplit("}", 1)[-1]
        if local_name != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if href:
            return href
        text = "".join(child.itertext()).strip()
        if text:
            return text
    return ""


def _market_relevance_score(title: str, source: str, source_kind: str) -> int:
    text = f"{title} {source}".lower()
    score = sum(1 for term in MARKET_RELEVANCE_TERMS if term in text)
    if source_kind in {
        "bank_of_canada",
        "federal_reserve",
        "bls",
        "bea",
        "eia",
        "sec",
    }:
        score += 4
    if any(term in title.lower() for term in LOW_SIGNAL_TITLE_TERMS):
        score -= 6
    return score


def _candidate_to_article(candidate: MarketNewsCandidate) -> dict:
    article_id = hashlib.sha256(f"{candidate.url}|{candidate.title}".encode("utf-8")).hexdigest()[:12]
    return {
        "id": article_id,
        "title": candidate.title,
        "url": candidate.url,
        "source": candidate.source,
        "published_at": candidate.published_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "image_url": candidate.image_url,
        "source_kind": candidate.source_kind,
    }


async def _fetch_rss_articles(
    client: httpx.AsyncClient,
    source: str,
    source_kind: str,
    feed_url: str,
) -> list[MarketNewsCandidate]:
    response = await client.get(feed_url, follow_redirects=True)
    response.raise_for_status()
    root = ElementTree.fromstring(response.content)
    candidates: list[MarketNewsCandidate] = []
    entries = [*root.findall(".//{*}item"), *root.findall(".//{*}entry")]
    if not entries:
        raise ValueError("feed contains no articles")
    for item in entries:
        title = _clean_text(_find_feed_text(item, "title"))
        url = _find_feed_link(item)
        article_source = source
        published_at = _parse_feed_date(_find_feed_text(item, "pubDate", "date", "published", "updated"))
        if not title or not url or not published_at:
            continue
        score = _market_relevance_score(title, article_source, source_kind)
        if score < 2:
            continue
        candidates.append(
            MarketNewsCandidate(
                title=title,
                url=url,
                source=article_source,
                published_at=published_at,
                image_url=None,
                source_kind=source_kind,
                score=score,
            )
        )
    return candidates


async def _collect_market_news_candidates() -> tuple[list[MarketNewsCandidate], bool]:
    timeout = httpx.Timeout(MARKET_NEWS_REQUEST_TIMEOUT)
    headers = {
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
        "User-Agent": "BreakTwenty/1.0 official-public-feed-reader",
    }
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
        tasks = [
            _fetch_rss_articles(client, source, source_kind, feed_url)
            for source, source_kind, feed_url in OFFICIAL_MARKET_NEWS_FEEDS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates: list[MarketNewsCandidate] = []
    partial = False
    for result in results:
        if isinstance(result, Exception):
            partial = True
            logger.warning("market news official feed fetch failed: %s", result.__class__.__name__)
            continue
        candidates.extend(result)
    return candidates, partial


def _dedupe_and_rank_candidates(candidates: list[MarketNewsCandidate]) -> list[MarketNewsCandidate]:
    deduped: dict[str, MarketNewsCandidate] = {}
    for candidate in candidates:
        parsed = urlparse(candidate.url)
        query = f"?{parsed.query.lower()}" if parsed.query else ""
        key = f"{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}{query}" or candidate.title.lower()
        existing = deduped.get(key)
        if existing is None or (candidate.published_at, candidate.score) > (existing.published_at, existing.score):
            deduped[key] = candidate
    return sorted(
        deduped.values(),
        key=lambda item: (item.published_at, item.score),
        reverse=True,
    )


def _select_market_news_candidates(candidates: list[MarketNewsCandidate]) -> list[MarketNewsCandidate]:
    selected: list[MarketNewsCandidate] = []
    selected_urls: set[str] = set()
    selected_sources: set[str] = set()
    for candidate in candidates:
        if candidate.source in selected_sources:
            continue
        selected.append(candidate)
        selected_urls.add(candidate.url)
        selected_sources.add(candidate.source)
        if len(selected) >= MARKET_NEWS_LIMIT:
            return selected
    for candidate in candidates:
        if candidate.url in selected_urls:
            continue
        selected.append(candidate)
        if len(selected) >= MARKET_NEWS_LIMIT:
            return selected
    return selected


async def get_market_news_payload() -> dict:
    global _market_news_cache

    now = datetime.now(timezone.utc)
    if _market_news_cache and now - _market_news_cache[0] <= _cache_ttl_for_payload(_market_news_cache[1]):
        return deepcopy(_market_news_cache[1])

    async with _market_news_lock:
        now = datetime.now(timezone.utc)
        if _market_news_cache and now - _market_news_cache[0] <= _cache_ttl_for_payload(_market_news_cache[1]):
            return deepcopy(_market_news_cache[1])

        raw_candidates, partial = await _collect_market_news_candidates()
        candidates = _dedupe_and_rank_candidates(raw_candidates)
        selected_candidates = _select_market_news_candidates(candidates)
        payload = {
            "status": "ok",
            "source": "official_public_feeds",
            "partial": partial,
            "limit": MARKET_NEWS_LIMIT,
            "fetched_at": now.isoformat().replace("+00:00", "Z"),
            "articles": [_candidate_to_article(candidate) for candidate in selected_candidates],
        }
        _market_news_cache = (now, payload)
        return deepcopy(payload)
