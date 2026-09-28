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
in-window match in one call per sport -- same "one bulk request" shape as
Betway ZA and WSB. Verified live with a bare `curl` GET (default UA, no
special headers, no cookies): HTTP 200, real JSON odds, every time,
including with zero headers at all. Plain HTTP GET, no TLS impersonation,
no stealth browser, no Cloudflare bypass -- none of that is needed here
because this specific endpoint (Kambi's own CloudFront-fronted API) isn't
behind bot detection, even though Sunbet's own site is.

Covers three sports -- soccer, rugby, and cricket (South Africa's #1-#3
sports by popularity) -- all via the same `listView` endpoint and query
shape, just a different sport slug in the URL path. Rugby's slug is
"rugby_union", not the more obvious "rugby" or "rugby-union" (both of those
404 with Kambi's own `{"error":{"message":"No event groups/participants are
matching the given terms","status":404}}`); found by trying plausible slugs
against the live endpoint with a bare curl, confirmed working with HTTP 200
and a real fixture (New Zealand vs Australia, Bledisloe Cup) whose own
`terms`/`path` entries self-report `"termKey":"rugby_union"`/
`"localizedName":"Rugby Union"`. Cricket's slug is simply "cricket" (the
naive guess, no brute-forcing needed this time) -- confirmed working with
HTTP 200 and a real live payload (Sri Lanka vs Nepal, Asian Games, among
others) whose own `path` entries self-report `"termKey":"cricket"`/
`"englishName":"Cricket"`. Kambi's per-record `event.sport` field
distinguishes all three ("FOOTBALL" vs "RUGBY_UNION" vs "CRICKET"), so
`to_odds_events` reads the universal sport off each record itself (via
KAMBI_SPORT_INFO below) rather than needing the caller to say which sport a
given raw payload came from -- same idiom as Betway ZA and Easybet's own
rugby support in this project.

Rugby's moneyline betOffer uses betOfferType "Match" same as soccer, but a
different criterion englishLabel: soccer's is "Full Time", rugby's is
"Regular Time" (rugby doesn't play "full time" the way soccer does -- no
extra time in a normal league or test match). The one live rugby fixture
inspected at discovery time (an international test, Bledisloe Cup) was a
genuine 2-way market -- only OT_ONE/OT_TWO outcomes, no OT_CROSS -- with an
explicit "Dead heat (odds divided by 2) applies in case of a draw" note in
the betOffer's `extra` field instead of a separate draw price. The same
payload's own `categoryGroups` also lists a "Match Odds (3-Way)" category,
confirming Kambi does price an explicit draw outcome for at least some
rugby competitions. The mapping logic below was already sport-agnostic on
this point for soccer (it never assumes OT_CROSS exists, `draw_odds` just
stays unset), so no special-casing was needed to make it handle both rugby
shapes correctly.

Cricket's moneyline betOffer also uses betOfferType "Match", but yet
another criterion englishLabel: "Match Odds" (neither soccer's "Full Time"
nor rugby's "Regular Time" -- cricket has no notion of either). Every live
cricket fixture inspected at discovery time (a listView pull with events
spanning international ODIs, South Africa's domestic Pro20 Cup T20
competition, and the Asian Games) was a genuine 2-way market -- only
OT_ONE/OT_TWO outcomes, no OT_CROSS anywhere in the payload -- consistent
with limited-overs cricket (ODI/T20), which unlike Test cricket cannot end
in a draw. The payload did include one Ashes ("Test") series entry, but it
was an outright/ante-post series-winner market with an empty `betOffers`
list (no match-level odds yet), not an actual 3-way Test match price, so a
genuine 3-way cricket market was not observed live at discovery time --
only noted as a theoretical possibility (Test cricket can be drawn) worth
re-checking if one ever shows up. As with rugby, the mapping logic needed
no special-casing to handle this: it never assumes OT_CROSS exists.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx
from ingestion.base_scraper import BaseScraper
from schemas import MarketOdds, OddsEvent

logger = logging.getLogger(__name__)

SUNBET_KAMBI_LISTVIEW_URL_TEMPLATE = (
    "https://eu-offering-api.kambicdn.com/offering/v2018/siwc/listView/{kambi_sport_slug}/all/all/all.json"
)

# The Kambi `listView` path segment for each sport this adapter polls. See
# the module docstring for how "rugby_union" was confirmed (and why the
# more obvious "rugby"/"rugby-union" guesses are wrong -- both 404), and how
# "cricket" (the naive guess) was confirmed to work as-is.
DEFAULT_KAMBI_SPORT_SLUGS = ("football", "rugby_union", "cricket")

# Kambi's per-record `event.sport` value -> the universal OddsEvent.sport
# name to publish, and which Match betOffer's criterion.englishLabel is
# that sport's full/regular-time/match-odds moneyline market (see module
# docstring).
KAMBI_SPORT_INFO = {
    "FOOTBALL": {"sport": "soccer", "moneyline_criterion": "Full Time"},
    "RUGBY_UNION": {"sport": "rugby", "moneyline_criterion": "Regular Time"},
    "CRICKET": {"sport": "cricket", "moneyline_criterion": "Match Odds"},
}

# Plain, fixed-interval polling -- same cadence as a normal page refresh,
# not randomized or disguised to look human. See the README's scope note.
DEFAULT_POLL_INTERVAL_SECONDS = 45

# Kambi's outcome "type" field for a 1X2/moneyline market on a Match
# betOffer. Not every sport/competition prices a draw (OT_CROSS) -- the
# parsing below never assumes it's present, see docstring.
OUTCOME_TYPE_HOME = "OT_ONE"
OUTCOME_TYPE_DRAW = "OT_CROSS"
OUTCOME_TYPE_AWAY = "OT_TWO"

OPEN_STATUS = "OPEN"

# Kambi represents decimal odds as an integer scaled by 1000 (e.g. 2950 ==
# 2.95 decimal odds).
ODDS_SCALE = 1000.0


class SunbetScraper(BaseScraper):
    bookmaker_id = "sunbet"

    def __init__(
        self,
        market: str = "ZA",
        client_id: str = "2",
        channel_id: str = "1",
        lang: str = "en_ZA",
        kambi_sport_slugs: tuple[str, ...] = DEFAULT_KAMBI_SPORT_SLUGS,
    ):
        self.market = market
        self.client_id = client_id
        self.channel_id = channel_id
        self.lang = lang
        self.kambi_sport_slugs = tuple(kambi_sport_slugs)
        self._client = httpx.AsyncClient(timeout=15)

    async def fetch_raw_odds(self, kambi_sport_slug: str) -> dict:
        response = await self._client.get(
            SUNBET_KAMBI_LISTVIEW_URL_TEMPLATE.format(kambi_sport_slug=kambi_sport_slug),
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
        the universal OddsEvent schema. Moneyline (Match / full-or-regular-
        time market) only for now, matching Betway ZA and WSB's current
        scope. Sport-agnostic -- it reads each record's own `event.sport`
        field (via KAMBI_SPORT_INFO) rather than trusting which sport slug
        the caller happened to fetch, so it works the same whether `raw`
        came from the football, rugby_union, or cricket listView call.

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

                sport_info = KAMBI_SPORT_INFO.get(event.get("sport"))
                if sport_info is None:
                    logger.warning(
                        "Skipping event %s with unrecognized Kambi sport %r", event_id, event.get("sport")
                    )
                    continue

                moneyline_offer = next(
                    (
                        bo
                        for bo in item.get("betOffers", [])
                        if bo.get("betOfferType", {}).get("name") == "Match"
                        and bo.get("criterion", {}).get("englishLabel") == sport_info["moneyline_criterion"]
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
                        sport=sport_info["sport"],
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
        """Fetches odds for every configured sport on a fixed interval and
        yields one combined batch of parsed events per cycle. Each sport is
        fetched and parsed independently, so a transient fetch failure for
        one sport (network blip, momentary 5xx, or a non-JSON error page
        served with a 200 status) is logged and skipped without blocking the
        other sport's fetch -- if soccer fails but rugby succeeds this
        cycle, rugby's events are still published. Only if *every* sport
        fails this cycle is nothing yielded at all, same as the original
        single-sport behaviour: that leaves the heartbeat un-refreshed so a
        real outage still surfaces as "down" downstream once the freshness
        TTL elapses, rather than being masked as an "ok, 0 events" cycle.
        Nothing from a single bad cycle should ever be allowed to kill this
        loop -- it's meant to run unattended for the life of the process.
        The freshness circuit breaker downstream is what protects against
        acting on odds that are actually stale, not this loop.
        """
        while True:
            batch: list[OddsEvent] = []
            any_success = False
            for kambi_sport_slug in self.kambi_sport_slugs:
                try:
                    raw = await self.fetch_raw_odds(kambi_sport_slug)
                    batch.extend(self.to_odds_events(raw))
                    any_success = True
                except httpx.HTTPError as exc:
                    logger.warning(
                        "%s: poll fetch failed for kambi_sport_slug=%s: %s", self.bookmaker_id, kambi_sport_slug, exc
                    )
                except Exception:
                    logger.exception(
                        "%s: unexpected error in poll cycle for kambi_sport_slug=%s", self.bookmaker_id, kambi_sport_slug
                    )

            if any_success:
                yield batch

            await asyncio.sleep(interval_seconds)

    async def close(self) -> None:
        await self._client.aclose()
