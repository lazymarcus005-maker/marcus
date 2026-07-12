import aio_pika
import redis.asyncio as redis
from fastapi import APIRouter
from sqlalchemy import text

from harness.config import get_settings
from harness.db.session import get_sessionmaker
from harness.observability import metrics_response

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness check — process is up. Does not touch dependencies."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    """Readiness check — verifies PostgreSQL, Redis, and RabbitMQ are reachable."""
    settings = get_settings()
    checks: dict[str, str] = {}

    try:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"error: {exc}"

    try:
        client = redis.from_url(settings.redis_url)
        await client.ping()
        await client.aclose()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"

    try:
        connection = await aio_pika.connect_robust(settings.rabbitmq_url, timeout=5)
        await connection.close()
        checks["rabbitmq"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["rabbitmq"] = f"error: {exc}"

    healthy = all(v == "ok" for v in checks.values())
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/metrics")
async def metrics():
    return metrics_response()
