"""
keymgr/app.py — FastAPI application for the key-management server (port 8092).

Issues one-month browser-access keys for the guest portal (port 8090).
Intended for LAN/internal use only — no authentication required.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import db

log = logging.getLogger(__name__)
from keymgr.router import router


@asynccontextmanager
async def _lifespan(app: FastAPI):  # noqa: ARG001
    await db.init_db()
    log.info("=== Key manager ready on port 8092 ===")
    yield


keymgr_app = FastAPI(
    title="EV Portal Key Manager",
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

keymgr_app.include_router(router)
