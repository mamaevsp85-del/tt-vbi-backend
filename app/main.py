from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.config import ROOT_DIR, settings
from app.core.database import init_db
from app.services import quota

(ROOT_DIR / "data").mkdir(parents=True, exist_ok=True)
(ROOT_DIR / "logs").mkdir(parents=True, exist_ok=True)
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
if not os.environ.get("RENDER"):
    _log_handlers.append(logging.FileHandler(ROOT_DIR / "logs" / "app.log", encoding="utf-8"))
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_log_handlers,
)

app = FastAPI(title=settings.app_name, version="35.1.0")
_cors_origins = settings.cors_origin_list or ["http://localhost:8080"]
if os.environ.get("RENDER"):
    _cors_origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api/v1")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.get("/")
def root():
    return {"name": settings.app_name, "version": "35.1.0", "code": "history-elo", "status": "running", "quota": quota.snapshot()}


@app.get("/health")
def health():
    return {"status": "healthy", "version": "35.1.0", "code": "history-elo", "quota": quota.snapshot()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.api_host, port=settings.api_port, reload=True)
