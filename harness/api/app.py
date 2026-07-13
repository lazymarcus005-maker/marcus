from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from harness.api.routes import (
    api_keys,
    approvals,
    health,
    llm_settings,
    mcp_servers,
    ops,
    runs,
    scheduled_jobs,
    skills,
    slack,
    slack_settings,
)
from harness.config import get_settings
from harness.observability import configure_observability


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="harness", version="1.0.0.1", lifespan=lifespan)
    app.state.settings = settings
    configure_observability(app, settings)

    app.include_router(health.router)
    app.include_router(api_keys.router)
    app.include_router(llm_settings.router)
    app.include_router(ops.router)
    app.include_router(runs.router)
    app.include_router(scheduled_jobs.router)
    app.include_router(mcp_servers.router)
    app.include_router(approvals.router)
    app.include_router(skills.router)
    app.include_router(slack.router)
    app.include_router(slack_settings.router)

    web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web_dist.exists():
        app.mount("/app", StaticFiles(directory=web_dist, html=True), name="web")

    return app


app = create_app()
