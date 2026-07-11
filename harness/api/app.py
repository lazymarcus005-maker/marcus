from contextlib import asynccontextmanager

from fastapi import FastAPI

from harness.api.routes import health, runs
from harness.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="harness", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    app.include_router(health.router)
    app.include_router(runs.router)

    return app


app = create_app()
