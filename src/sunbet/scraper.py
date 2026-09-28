"""Sunbet adapter.

Sunbet's sportsbook is a Kambi turnkey platform (`class="kambi-client"`,
`data-kambi_url="https://static.kambicdn.com/client/siwc/kambi-bootstrap.js"`,
`data-kambi_env="prod"` on https://play.sunbet.co.za). The betting UI itself
is loaded behind Sunbet's own Cloudflare, but the odds it displays are
fetched client-side, straight from Kambi's own AWS/CloudFront-hosted public
offering API -- not from anything behind Sunbet's Cloudflare, no session,
no auth. The exact call (and Sunbet's client_id) was found in a plain,
unminified-enough read of Sunbet's own AEM clientlibs-main.js bundle
(https://play.sunbet.co.za/etc.clientlibs/sunbet/clientlibs/clientlibs-main.lc-*.min.js),
which does this for its own single-event odds refresh:

    $.ajax({
        url: 'https://eu-offering-api.kambicdn.com/offering/v2018/siwc/betoffer/event/'
             + eventId + '.json?lang=en_ZA&market=ZA&client_id=2&channel_id=1&ncid=...'
             + '&includeParticipants=true',
        ...
    });

This adapter uses Kambi's equivalent bulk `listView` endpoint (same host,
same query params) instead of the single-event one, to fetch every
in-window soccer match in one call -- same "one bulk request" shape as
Betway ZA and WSB. Verified live with a bare `curl` GET (default UA, no
special headers, no cookies): HTTP 200, real JSON odds, every time,
including with zero headers at all. Plain HTTP GET, no TLS impersonation,
no stealth browser, no Cloudflare bypass -- none of that is needed here
because this specific endpoint (Kambi's own CloudFront-fronted API) isn't
behind bot detection, even though Sunbet's own site is.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
from ingestion.base_scraper import BaseScraper
from schemas import MarketOdds, OddsEvent

logger = logging.getLogger(__name__)

SUNBET_KAMBI_LISTVIEW_URL = "https://eu-offering-api.kambicdn.com/offering/v2018/siwc/listView/football/all/all/all.json"

# Plain, fixed-interval polling -- same cadence as a normal page refresh,
# not randomized or disguised to look human. See the README's scope note.
DEFAULT_POLL_INTERVAL_SECONDS = 45

# Kambi's outcome "type" field for a 1X2 (moneyline) market on a Match/Full
# Time betOffer.
OUTCOME_TYPE_HOME = "OT_ONE"
OUTCOME_TYPE_DRAW = "OT_CROSS"
OUTCOME_TYPE_AWAY = "OT_TWO"

OPEN_STATUS = "OPEN"

# Kambi represents decimal odds as an integer scaled by 1000 (e.g. 2950 ==
# 2.95 decimal odds).
ODDS_SCALE = 1000.0


class SunbetScraper(BaseScraper):
    bookmaker_id = "sunbet"

    def __init__(self, market: str = "ZA", client_id: str = "2", channel_id: str = "1", lang: str = "en_ZA"):
        self.market = market
        self.client_id = client_id
        self.channel_id = channel_id
        self.lang = lang
        self._client = httpx.AsyncClient(timeout=15)

    async def fetch_raw_odds(self) -> dict:
        response = await self._client.get(
            SUNBET_KAMBI_LISTVIEW_URL,
            params={
                "lang": self.lang,
                "market": self.market,
                "client_id": self.client_id,
                "channel_id": self.channel_id,
                "includeParticipants": "true",
            },
        )
        response.raise_for_status()
        return response.json()

    def to_odds_events(self, raw: dict) -> list[OddsEvent]:
        """Maps Kambi's nested `events[].{event,betOffers}` structure onto
        the universal OddsEvent schema. Moneyline (Match / Full Time market)
        only for now, matching Betway ZA and WSB's current scope.

        Note event_id (the OddsEvent field) is left unset here -- that's the
        engine's job downstream (see the note on OddsEvent.event_id in
        scanner-schemas).
        """
        scraped_at = datetime.now(timezone.utc)
        odds_events: list[OddsEvent] = []

        for item in raw.get("events", []):
            # Defensive against a single malformed record (missing/odd field
            # shape): skip and log just that record rather than let one bad
            # entry in a ~200-item bulk payload crash the whole poll cycle.
            try:
                event = item.get("event", {})
                event_id = event["id"]
                home_team = event["homeName"]
                away_team = event["awayName"]

                moneyline_offer = next(
                    (
                        bo
                        for bo in item.get("betOffers", [])
                        if bo.get("betOfferType", {}).get("name") == "Match"
                        and bo.get("criterion", {}).get("englishLabel") == "Full Time"
                    ),
                    None,
                )
                if moneyline_offer is None:
                    continue

                home_odds = away_odds = draw_odds = None
                for outcome in moneyline_offer.get("outcomes", []):
                    if outcome.get("status") != OPEN_STATUS:
                        continue
                    odds_value = outcome.get("odds")
                    if odds_value is None:
                        continue
                    decimal_odds = odds_value / ODDS_SCALE

                    outcome_type = outcome.get("type")
                    if outcome_type == OUTCOME_TYPE_HOME:
                        home_odds = decimal_odds
                    elif outcome_type == OUTCOME_TYPE_AWAY:
                        away_odds = decimal_odds
                    elif outcome_type == OUTCOME_TYPE_DRAW:
                        draw_odds = decimal_odds

                if home_odds is None or away_odds is None:
                    continue  # incomplete market, don't publish a partial price

                try:
                    start_time = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    continue

                odds_events.append(
                    OddsEvent(
                        sport="soccer",
                        league=event.get("group", "unknown"),
                        home_team=home_team,
                        away_team=away_team,
                        start_time=start_time,
                        bookmaker=self.bookmaker_id,
                        markets={
                            "moneyline": MarketOdds(home_odds=home_odds, away_odds=away_odds, draw_odds=draw_odds)
                        },
                        scraped_at=scraped_at,
                    )
                )
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                logger.warning("Skipping malformed event %s: %s", item.get("event", {}).get("id"), exc)
                continue

        return odds_events

    async def poll(
        self, interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    ) -> AsyncIterator[list[OddsEvent]]:
        """Fetches odds on a fixed interval and yields the parsed events each
        time. A transient fetch failure (network blip, momentary 5xx, or a
        non-JSON error page served with a 200 status) is logged and the loop
        continues on schedule rather than crashing -- this loop is meant to
        run unattended for the life of the process, so nothing from a single
        bad cycle should ever be allowed to kill it. The freshness circuit
        breaker downstream is what protects against acting on odds that are
        actually stale, not this loop.
        """
        while True:
            try:
                raw = await self.fetch_raw_odds()
                yield self.to_odds_events(raw)
            except httpx.HTTPError as exc:
                logger.warning("%s: poll fetch failed: %s", self.bookmaker_id, exc)
            except Exception:
                logger.exception("%s: unexpected error in poll cycle", self.bookmaker_id)

            await asyncio.sleep(interval_seconds)

    async def close(self) -> None:
        await self._client.aclose()
