"""Serves the small single-page web UI (app/static/index.html) at /ui."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)

_PAGE = Path(__file__).resolve().parents[2] / "static" / "index.html"


@router.get("/ui")
def ui() -> FileResponse:
    return FileResponse(_PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})
