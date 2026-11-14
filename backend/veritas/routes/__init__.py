"""API routes, one module per area. Add each new router to ROUTERS; main.py mounts them under /api."""
from . import health, screens

ROUTERS = [health.router, screens.router]
