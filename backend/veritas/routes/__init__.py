"""API routes, one module per area. Add each new router to ROUTERS; main.py mounts them under /api."""
from . import analyze, health, screens, system

ROUTERS = [health.router, screens.router, analyze.router, system.router]
