"""
admin/app.py – FastAPI application instance for the admin HTTPS server.

This app is started as a second uvicorn server by serve.py.
It shares state (DB, Square config) with the guest app via state.py.
"""

from fastapi import FastAPI

from admin.router import router

admin_app = FastAPI(title="EV Portal Admin", docs_url="/admin/docs", redoc_url=None)
admin_app.include_router(router)
