"""ASGI entry point for Uvicorn or Gunicorn."""

from athena.api.server import create_app

app = create_app()
