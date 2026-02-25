"""
EV Charger Portal - application entry point.

All logic lives in:
  state.py                 - shared globals
  mqtt.py                  - MQTT client factory
  square.py                - Square API helpers
  lifespan.py              - startup / shutdown
  endpoints/index.py       - GET /
  endpoints/health.py      - GET /health
  endpoints/debug.py       - GET /debug
  endpoints/start.py       - GET /start
  endpoints/payment_post_process.py - GET /payment_post_process
"""

import logging

from fastapi import FastAPI

from lifespan import lifespan
from endpoints import debug, health, index, payment_post_process, start

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

app = FastAPI(title="EV Charger Portal", lifespan=lifespan)

app.include_router(index.router)
app.include_router(health.router)
app.include_router(debug.router)
app.include_router(start.router)
app.include_router(payment_post_process.router)
