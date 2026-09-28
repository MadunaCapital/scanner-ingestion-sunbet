import pytest

from sunbet import SunbetScraper

# Shape captured from a real, plain GET to Sunbet's Kambi-hosted `listView`
# endpoint (see scraper.py's docstring). Trimmed to one match's worth of
# records, field names and values unchanged from the real response.
SAMPLE_RAW_PAYLOAD = {
    "events": [
        {
            "event": {
                "id": 1028648396,
                "name": "Leixoes U23 - FC Vizela U23",
                "homeName": "Leixoes U23",
                "awayName": "FC Vizela U23",
                "start": "2026-09-28T15:00:00Z",
                "group": "Liga Revelacao U23",
                "sport": "FOOTBALL",
                "state": "NOT_STARTED",
            },
            "betOffers": [
                {
                    "id": 2696504134,
                    "criterion": {"id": 1001159858, "label": "Full Time", "englishLabel": "Full Time"},
                    "betOfferType": {"id": 2, "name": "Match"},
                    "eventId": 1028648396,
                    "outcomes": [
                        {
                            "id": 4350854988,
                            "label": "1",
                            "odds": 2200,
                            "type": "OT_ONE",
                            "status": "OPEN",
                            "betOfferId": 2696504134,
                        },
                        {
                            "id": 4350854989,
                            "label": "X",
                            "odds": 3350,
                            "type": "OT_CROSS",
                            "status": "OPEN",
                            "betOfferId": 2696504134,
                        },
                        {
                            "id": 4350854990,
                            "label": "2",
                            "odds": 2700,
                            "type": "OT_TWO",
                            "status": "OPEN",
                            "betOfferId": 2696504134,
                        },
                    ],
                }
            ],
        }
    ]
}


def test_to_odds_events_maps_kambi_event_and_betoffer_structure():
    scraper = SunbetScraper()

    events = scraper.to_odds_events(SAMPLE_RAW_PAYLOAD)

    assert len(events) == 1
    event = events[0]
    assert event.sport == "soccer"
    assert event.league == "Liga Revelacao U23"
    assert event.home_team == "Leixoes U23"
    assert event.away_team == "FC Vizela U23"
    assert event.bookmaker == "sunbet"
    assert event.event_id is None  # left for the engine to compute
    # Kambi odds are scaled by 1000 (2200 -> 2.20 decimal odds).
    assert event.markets["moneyline"].home_odds == 2.2
    assert event.markets["moneyline"].away_odds == 2.7
    assert event.markets["moneyline"].draw_odds == 3.35


def test_to_odds_events_skips_events_without_a_match_full_time_offer():
    payload = {
        "events": [
            {
                **SAMPLE_RAW_PAYLOAD["events"][0],
                "betOffers": [
                    {
                        **SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0],
                        "betOfferType": {"id": 9, "name": "Handicap"},
                    }
                ],
            }
        ]
    }
    scraper = SunbetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_suspended_outcomes():
    """A SUSPENDED outcome must not be treated as a live price -- the market
    should come through incomplete (and therefore skipped) rather than
    publish a stale/frozen price."""
    payload = {
        "events": [
            {
                **SAMPLE_RAW_PAYLOAD["events"][0],
                "betOffers": [
                    {
                        **SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0],
                        "outcomes": [
                            {**SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0]["outcomes"][0], "status": "SUSPENDED"},
                            SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0]["outcomes"][1],
                            SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0]["outcomes"][2],
                        ],
                    }
                ],
            }
        ]
    }
    scraper = SunbetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_incomplete_price_data():
    """If home or away is missing a price, don't publish a partial/misleading market."""
    payload = {
        "events": [
            {
                **SAMPLE_RAW_PAYLOAD["events"][0],
                "betOffers": [
                    {
                        **SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0],
                        "outcomes": [SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0]["outcomes"][0]],  # home only
                    }
                ],
            }
        ]
    }
    scraper = SunbetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_handles_multiple_events_independently():
    second_event = {
        "event": {
            **SAMPLE_RAW_PAYLOAD["events"][0]["event"],
            "id": 999999,
            "homeName": "TeamA",
            "awayName": "TeamB",
        },
        "betOffers": [
            {
                **SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0],
                "eventId": 999999,
                "outcomes": [
                    {**SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0]["outcomes"][0], "odds": 1500},
                    {**SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0]["outcomes"][1], "odds": 3500},
                    {**SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0]["outcomes"][2], "odds": 2500},
                ],
            }
        ],
    }
    payload = {"events": [SAMPLE_RAW_PAYLOAD["events"][0], second_event]}
    scraper = SunbetScraper()

    events = scraper.to_odds_events(payload)

    assert len(events) == 2
    assert {e.home_team for e in events} == {"Leixoes U23", "TeamA"}


def test_to_odds_events_skips_a_malformed_event_without_crashing_the_batch():
    """One event missing a required field (e.g. homeName) must not take
    down parsing of every other event in the same ~200-item bulk payload."""
    malformed_event = {
        "event": {
            "id": 999999,
            "name": "Malformed",
            # homeName deliberately missing
            "awayName": "TeamB",
            "start": "2026-09-28T15:00:00Z",
            "group": "Some League",
            "sport": "FOOTBALL",
            "state": "NOT_STARTED",
        },
        "betOffers": [
            {
                **SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"][0],
                "eventId": 999999,
            }
        ],
    }
    payload = {"events": [SAMPLE_RAW_PAYLOAD["events"][0], malformed_event]}
    scraper = SunbetScraper()

    events = scraper.to_odds_events(payload)

    # The well-formed event still comes through; the malformed one is
    # skipped rather than raising and losing the whole batch.
    assert len(events) == 1
    assert events[0].home_team == "Leixoes U23"


def test_to_odds_events_skips_a_malformed_start_timestamp():
    payload = {
        "events": [
            {
                "event": {**SAMPLE_RAW_PAYLOAD["events"][0]["event"], "start": "not-a-timestamp"},
                "betOffers": SAMPLE_RAW_PAYLOAD["events"][0]["betOffers"],
            }
        ]
    }
    scraper = SunbetScraper()

    events = scraper.to_odds_events(payload)

    assert events == []


@pytest.mark.asyncio
async def test_poll_yields_events_on_a_fixed_interval(monkeypatch):
    scraper = SunbetScraper()

    async def fake_fetch_raw_odds():
        return SAMPLE_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", fake_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        if len(results) == 3:
            break

    assert len(results) == 3
    assert all(len(batch) == 1 and batch[0].home_team == "Leixoes U23" for batch in results)


@pytest.mark.asyncio
async def test_poll_continues_past_a_transient_fetch_failure(monkeypatch):
    import httpx

    scraper = SunbetScraper()
    call_count = 0

    async def flaky_fetch_raw_odds():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("simulated network blip")
        return SAMPLE_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", flaky_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        break  # first successful yield should be the second call, after the failure

    assert call_count == 2
    assert len(results) == 1
    assert results[0][0].home_team == "Leixoes U23"
