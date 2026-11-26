from fastapi import FastAPI

from app.api.health import router as health_router
from app.api.videos import router as videos_router
from app.core.body_limit import BodyLimitMiddleware
from app.core.config import settings
from app.core.logging import configure_logging


def create_app():
    configure_logging()
    app = FastAPI(title="AI Travel Video Service", version="0.1.0")
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings().max_request_bytes)
    app.include_router(health_router)
    app.include_router(videos_router)
    return app

