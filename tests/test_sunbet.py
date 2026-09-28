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


# Shape captured from a real, plain GET to Sunbet's Kambi-hosted `listView`
# endpoint with the "rugby_union" sport slug (see scraper.py's module
# docstring for how that slug was confirmed -- "rugby"/"rugby-union" both
# 404). Trimmed to one match's worth of records, field names and values
# unchanged from the real response. Notably a genuine 2-way market: this
# international test match has only OT_ONE/OT_TWO outcomes, no OT_CROSS --
# Kambi applies a "dead heat" (odds halved) rule for a draw instead of
# pricing a separate draw outcome, per the betOffer's own "extra" field.
RUGBY_TWO_WAY_RAW_PAYLOAD = {
    "events": [
        {
            "event": {
                "id": 1029297044,
                "name": "New Zealand - Australia",
                "homeName": "New Zealand",
                "awayName": "Australia",
                "start": "2026-10-10T06:10:00Z",
                "group": "International",
                "sport": "RUGBY_UNION",
                "state": "NOT_STARTED",
                "extraInfo": "Bledisloe Cup",
            },
            "betOffers": [
                {
                    "id": 2697501509,
                    "criterion": {"id": 1001159767, "label": "Regular Time", "englishLabel": "Regular Time"},
                    "extra": "\"Dead heat\" rule (odds divided by 2) applies in case of a draw",
                    "betOfferType": {"id": 2, "name": "Match"},
                    "eventId": 1029297044,
                    "outcomes": [
                        {
                            "id": 4353970476,
                            "label": "New Zealand",
                            "odds": 1160,
                            "type": "OT_ONE",
                            "status": "OPEN",
                            "betOfferId": 2697501509,
                        },
                        {
                            "id": 4353970478,
                            "label": "Australia",
                            "odds": 5100,
                            "type": "OT_TWO",
                            "status": "OPEN",
                            "betOfferId": 2697501509,
                        },
                    ],
                }
            ],
        }
    ]
}


# Representative of a rugby union competition where Kambi *does* price an
# explicit draw outcome (OT_CROSS) rather than applying the dead-heat rule
# seen above -- the same real listView response's own `categoryGroups`
# lists a "Match Odds (3-Way)" category alongside the 2-way test match
# above, confirming both shapes exist on this endpoint. Hand-built from the
# same real field shapes (not itself a captured response) specifically to
# exercise the 3-way path, since only a 2-way fixture happened to be live
# at discovery time.
RUGBY_THREE_WAY_RAW_PAYLOAD = {
    "events": [
        {
            "event": {
                "id": 1029300001,
                "name": "Western Province - Blue Bulls",
                "homeName": "Western Province",
                "awayName": "Blue Bulls",
                "start": "2026-10-11T17:00:00Z",
                "group": "Currie Cup",
                "sport": "RUGBY_UNION",
                "state": "NOT_STARTED",
            },
            "betOffers": [
                {
                    "id": 2697600001,
                    "criterion": {"id": 1001159768, "label": "Regular Time", "englishLabel": "Regular Time"},
                    "betOfferType": {"id": 2, "name": "Match"},
                    "eventId": 1029300001,
                    "outcomes": [
                        {
                            "id": 4353980001,
                            "label": "Western Province",
                            "odds": 1400,
                            "type": "OT_ONE",
                            "status": "OPEN",
                            "betOfferId": 2697600001,
                        },
                        {
                            "id": 4353980002,
                            "label": "Draw",
                            "odds": 1500,
                            "type": "OT_CROSS",
                            "status": "OPEN",
                            "betOfferId": 2697600001,
                        },
                        {
                            "id": 4353980003,
                            "label": "Blue Bulls",
                            "odds": 3200,
                            "type": "OT_TWO",
                            "status": "OPEN",
                            "betOfferId": 2697600001,
                        },
                    ],
                }
            ],
        }
    ]
}


def test_to_odds_events_maps_rugby_two_way_market_with_no_draw():
    """Rugby's international-test 2-way market (no OT_CROSS) must map with
    draw_odds left unset, not force a fake 3-way structure."""
    scraper = SunbetScraper()

    events = scraper.to_odds_events(RUGBY_TWO_WAY_RAW_PAYLOAD)

    assert len(events) == 1
    event = events[0]
    assert event.sport == "rugby"
    assert event.league == "International"
    assert event.home_team == "New Zealand"
    assert event.away_team == "Australia"
    assert event.bookmaker == "sunbet"
    assert event.event_id is None
    assert event.markets["moneyline"].home_odds == 1.16
    assert event.markets["moneyline"].away_odds == 5.1
    assert event.markets["moneyline"].draw_odds is None


def test_to_odds_events_maps_rugby_three_way_market_with_a_draw():
    """Some rugby competitions do price an explicit draw -- when OT_CROSS is
    present it must come through as draw_odds, same as soccer."""
    scraper = SunbetScraper()

    events = scraper.to_odds_events(RUGBY_THREE_WAY_RAW_PAYLOAD)

    assert len(events) == 1
    event = events[0]
    assert event.sport == "rugby"
    assert event.league == "Currie Cup"
    assert event.markets["moneyline"].home_odds == 1.4
    assert event.markets["moneyline"].away_odds == 3.2
    assert event.markets["moneyline"].draw_odds == 1.5


def test_to_odds_events_skips_rugby_events_without_a_match_regular_time_offer():
    payload = {
        "events": [
            {
                **RUGBY_TWO_WAY_RAW_PAYLOAD["events"][0],
                "betOffers": [
                    {
                        **RUGBY_TWO_WAY_RAW_PAYLOAD["events"][0]["betOffers"][0],
                        "betOfferType": {"id": 9, "name": "Handicap"},
                    }
                ],
            }
        ]
    }
    scraper = SunbetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_skips_events_with_an_unrecognized_kambi_sport():
    """A sport this adapter doesn't cover (e.g. tennis) must be skipped
    rather than crash or be mis-published under an existing sport name."""
    payload = {
        "events": [
            {
                **RUGBY_TWO_WAY_RAW_PAYLOAD["events"][0],
                "event": {**RUGBY_TWO_WAY_RAW_PAYLOAD["events"][0]["event"], "sport": "TENNIS"},
            }
        ]
    }
    scraper = SunbetScraper()

    assert scraper.to_odds_events(payload) == []


def test_to_odds_events_handles_soccer_and_rugby_in_the_same_batch_independently():
    """A single poll cycle now combines both sports' raw payloads (see
    poll()) before to_odds_events ever sees them separately -- but
    to_odds_events itself is also exercised directly here with a payload
    holding one of each, confirming the per-event sport lookup (and its
    matching moneyline criterion) doesn't leak state between events of
    different sports in one batch."""
    payload = {"events": [SAMPLE_RAW_PAYLOAD["events"][0], RUGBY_TWO_WAY_RAW_PAYLOAD["events"][0]]}
    scraper = SunbetScraper()

    events = scraper.to_odds_events(payload)

    assert len(events) == 2
    by_sport = {e.sport: e for e in events}
    assert by_sport["soccer"].home_team == "Leixoes U23"
    assert by_sport["soccer"].markets["moneyline"].draw_odds == 3.35
    assert by_sport["rugby"].home_team == "New Zealand"
    assert by_sport["rugby"].markets["moneyline"].draw_odds is None


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
    # Single-sport scraper here so this exercises exactly the same
    # single-fetch-per-cycle behaviour the original soccer-only scraper had.
    scraper = SunbetScraper(kambi_sport_slugs=("football",))

    async def fake_fetch_raw_odds(kambi_sport_slug):
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

    scraper = SunbetScraper(kambi_sport_slugs=("football",))
    call_count = 0

    async def flaky_fetch_raw_odds(kambi_sport_slug):
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


@pytest.mark.asyncio
async def test_poll_fetches_every_configured_sport_and_merges_results(monkeypatch):
    """The default scraper polls both soccer and rugby every cycle, as two
    separate requests to the same endpoint, and publishes them together as
    one batch."""
    scraper = SunbetScraper()  # default kambi_sport_slugs: football + rugby_union
    requested_slugs = []

    async def fake_fetch_raw_odds(kambi_sport_slug):
        requested_slugs.append(kambi_sport_slug)
        return SAMPLE_RAW_PAYLOAD if kambi_sport_slug == "football" else RUGBY_TWO_WAY_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", fake_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        break

    assert requested_slugs == ["football", "rugby_union"]
    assert len(results) == 1
    batch = results[0]
    assert len(batch) == 2
    assert {e.sport for e in batch} == {"soccer", "rugby"}
    assert {e.home_team for e in batch} == {"Leixoes U23", "New Zealand"}


@pytest.mark.asyncio
async def test_poll_still_publishes_the_other_sport_when_one_sports_fetch_fails(monkeypatch):
    import httpx

    scraper = SunbetScraper()  # default kambi_sport_slugs: football + rugby_union

    async def fake_fetch_raw_odds(kambi_sport_slug):
        if kambi_sport_slug == "football":
            raise httpx.ConnectError("simulated soccer outage")
        return RUGBY_TWO_WAY_RAW_PAYLOAD

    monkeypatch.setattr(scraper, "fetch_raw_odds", fake_fetch_raw_odds)

    results = []
    async for events in scraper.poll(interval_seconds=0.01):
        results.append(events)
        break

    assert len(results) == 1
    batch = results[0]
    assert len(batch) == 1
    assert batch[0].sport == "rugby"


@pytest.mark.asyncio
async def test_poll_skips_the_yield_when_every_sport_fails_this_cycle(monkeypatch):
    """If nothing could be fetched this cycle at all, poll() must not yield
    an empty batch -- that would make run_scraper_loop write an "ok, 0
    events" heartbeat during a real outage, masking it instead of letting
    the heartbeat go stale and surface as down."""
    import asyncio

    import httpx

    scraper = SunbetScraper()  # default kambi_sport_slugs: football + rugby_union
    call_count = 0

    async def always_fails(kambi_sport_slug):
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("simulated total outage")

    monkeypatch.setattr(scraper, "fetch_raw_odds", always_fails)

    agen = scraper.poll(interval_seconds=0.01)
    # poll() should never yield while every fetch keeps failing -- bound the
    # wait so this fails loudly instead of hanging if that premise breaks.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(agen.__anext__(), timeout=0.5)

    # Several full cycles (2 sport fetches each) should have run in that
    # window, all failing, with nothing ever yielded.
    assert call_count >= 2
