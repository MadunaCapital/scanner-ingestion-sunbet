# scanner-ingestion-sunbet

Sunbet odds scraper for the MadunaCapital arbitrage scanner. One of several independent per-bookmaker repos, kept maximally decoupled from the others.

## How it works

Sunbet's sportsbook is a **Kambi** turnkey platform (`class="kambi-client"` on https://play.sunbet.co.za, bootstrapped from `static.kambicdn.com`). Sunbet's own site is Cloudflare-fronted, but the odds it displays are fetched client-side straight from **Kambi's own public offering API** (`eu-offering-api.kambicdn.com`, AWS/CloudFront-hosted) — not from anything behind Sunbet's Cloudflare, no session, no auth. The exact call and Sunbet's `client_id` were found in a plain read of Sunbet's own AEM `clientlibs-main.js` bundle, which does this for its own odds refresh:

```
GET https://eu-offering-api.kambicdn.com/offering/v2018/siwc/betoffer/event/{eventId}.json
    ?lang=en_ZA&market=ZA&client_id=2&channel_id=1&includeParticipants=true
```

This adapter uses Kambi's equivalent bulk `listView` endpoint (same host, same query params) to fetch every in-window soccer match in one call:

```
GET https://eu-offering-api.kambicdn.com/offering/v2018/siwc/listView/football/all/all/all.json
    ?lang=en_ZA&market=ZA&client_id=2&channel_id=1&includeParticipants=true
```

Verified live with a bare `curl` GET — default UA, no headers at all, no cookies: HTTP 200, real JSON odds, every time (177 events in one call during testing). Plain `httpx` GET, no TLS impersonation, no stealth browser, no Cloudflare bypass: none of that is needed because this specific endpoint (Kambi's own CloudFront-fronted API) isn't behind bot detection, even though Sunbet's own site is. Polls on a plain, fixed 45-second interval by default — no jitter, no randomization to look human.

Depends on:
- [scanner-ingestion](https://github.com/MadunaCapital/scanner-ingestion) (base install only, no `stealth` extra — this adapter doesn't need it) for `BaseScraper`
- [scanner-schemas](https://github.com/MadunaCapital/scanner-schemas) for `OddsEvent`/`MarketOdds`

Both pulled in as git dependencies in `requirements.txt`, same pattern as every other repo in this project.

## Response shape

Kambi's `listView` payload is `{"events": [{"event": {...}, "betOffers": [...], "liveData": {...}}, ...]}`, one entry per match:

- `event.id` / `event.homeName` / `event.awayName` / `event.start` (ISO 8601) / `event.group` (league/competition name)
- `betOffers[]` — filtered to the one where `betOfferType.name == "Match"` and `criterion.englishLabel == "Full Time"` (Kambi's 1X2/moneyline market)
- Each outcome has `type` (`OT_ONE` = home, `OT_CROSS` = draw, `OT_TWO` = away), `status` (`OPEN`/`SUSPENDED`), and `odds` — a **decimal price scaled by 1000** (e.g. `2200` → `2.20`)

A `SUSPENDED` outcome, or any outcome with no price, is treated as missing — the market is skipped rather than publishing a stale or partial price.

`event_id` (the `OddsEvent` field) is left unset — that's the engine's job downstream (see the note on `OddsEvent.event_id` in scanner-schemas).

## Status

Working and verified live: `SunbetScraper().fetch_raw_odds()` + `.to_odds_events()` returns real current soccer matches (prematch and live) with sane decimal odds, straight from Kambi's public API. 9 passing tests, including the real event/betOffer mapping, edge cases (suspended outcomes, missing market, incomplete prices), and the polling loop (fixed interval, continues past transient fetch failures).

Note: Sunbet's `listView` feed mixes real-world fixtures with 24/7 simulated "esports football" markets (e.g. "Cyber Live Arena", "Esports Battle") under the same `football` sport bucket — both have the same `event`/`betOffers` shape, so no extra filtering was needed for parsing, but downstream consumers should be aware the feed isn't exclusively real-world matches.

## Local dev

```
pip install -r requirements.txt
pytest
```

## Scope note

This adapter intentionally only reads what Kambi's own public offering API already serves publicly (the same data Sunbet's own frontend fetches to render its odds board), at a reasonable polling interval — no authentication bypass, no anti-bot evasion. Terms of Service exposure for scraping public data is a real but different (lower-severity, contractual rather than computer-misuse) question than the Cybercrimes Act question that applies to defeating security measures.
