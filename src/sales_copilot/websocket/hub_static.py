"""Static asset mounting for the hub app."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sales_copilot.core.paths import resolve_app_resource


def mount_static_apps(app: FastAPI, *, project_root: Path | None = None) -> None:
    if project_root is not None:
        root = project_root
    else:
        root = resolve_app_resource(".")
    dashboard_dir = root / "dashboard"
    presentation_dir = root / "presentation"

    if dashboard_dir.is_dir():
        app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")
    if presentation_dir.is_dir():
        app.mount("/presentation", StaticFiles(directory=str(presentation_dir), html=True), name="presentation")
