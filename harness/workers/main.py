import asyncio
import logging

from harness.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("harness.worker")


async def main() -> None:
    settings = get_settings()
    logger.info("worker starting", extra={"env": settings.env})
    while True:
        await asyncio.sleep(30)
        logger.info("worker heartbeat")


if __name__ == "__main__":
    asyncio.run(main())
