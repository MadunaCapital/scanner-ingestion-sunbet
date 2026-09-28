"""Entrypoint: `python -m sunbet`. Polls Sunbet and publishes each batch of
odds to Redis for scanner-engine to aggregate and detect arbitrage against
other bookmakers.
"""

import asyncio
import logging
import os

from ingestion.raw_publisher import run_scraper_loop
from redis.asyncio import from_url

from sunbet import SunbetScraper

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    redis = from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
    scraper = SunbetScraper()
    try:
        await run_scraper_loop(scraper, redis)
    finally:
        await scraper.close()


if __name__ == "__main__":
    asyncio.run(main())
