"""
portal_templates.py – Shared Jinja2Templates instance for the guest portal.

Import `templates` here rather than constructing it in each endpoint so all
views share one instance and its loader/cache.
"""

from pathlib import Path

from fastapi.templating import Jinja2Templates

_APP_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))
