"""
admin/app.py – FastAPI application instance for the admin HTTPS server.

This app is started as a second uvicorn server by serve.py.
It shares state (DB, Square config) with the guest app via state.py.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from admin.router import router

# docs_url and openapi_url are disabled here; protected versions are
# served by the router so they require admin credentials.
admin_app = FastAPI(
    title="EV Portal Admin",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_APP_DIR = Path(__file__).parent.parent
admin_app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")

admin_app.include_router(router)
