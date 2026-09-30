"""Upload routes for hub client assets."""

from __future__ import annotations

import stat
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from sales_copilot.core.cloud_sync_warning import klanten_root_cloud_sync_warning
from sales_copilot.core.context_docs import UPLOAD_ROOT, list_client_slugs, slugify_client_name
from sales_copilot.core.klant_config import KlantConfigError, load_klant_config
from sales_copilot.websocket.hub_auth import require_token

router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".txt", ".json", ".md"}


@router.post("/upload", dependencies=[Depends(require_token)])
async def upload_file(
    file: UploadFile = File(...),
    company_slug: str = Form("default"),
) -> dict[str, str]:
    filename = Path(file.filename or "").name
    if not filename:
        raise HTTPException(status_code=400, detail="Missing file upload")

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type")
    file_bytes = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")

    company_slug = slugify_client_name(company_slug)
    target_dir = UPLOAD_ROOT / company_slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(stat.S_IRWXU)
    target_path = target_dir / f"{uuid.uuid4().hex}{ext}"
    target_path.write_bytes(file_bytes)
    target_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    return {"id": target_path.relative_to(UPLOAD_ROOT).as_posix(), "name": filename}


@router.get("/api/v1/clients")
async def list_clients() -> dict[str, Any]:
    """List existing clients plus the klantmap-als-eenheid setup-menu warning (D2).

    Read-only, unauthenticated like the other listing endpoints (hub is
    localhost-only) -- returns folder slugs, each with a display name (``bedrijf``
    read from ``klant.yaml`` when present, else the slug itself), plus the
    cloud-sync warning for the currently configured ``KLANTEN_ROOT`` (D4) so the
    setup menu can show it once for the whole picker rather than per client.

    A client folder with an invalid ``klant.yaml`` still appears in the list (with the
    slug as its display name) rather than breaking the whole picker -- the same
    backward-compatible spirit as ``load_klant_config`` returning ``None`` for a folder
    without one.
    """
    clients: list[dict[str, str]] = []
    for slug in list_client_slugs():
        bedrijf = slug
        try:
            klant = load_klant_config(slug)
        except KlantConfigError:
            klant = None
        if klant is not None:
            bedrijf = klant.bedrijf
        clients.append({"slug": slug, "bedrijf": bedrijf})

    warning = klanten_root_cloud_sync_warning()
    cloud_sync_warning = (
        {"kind": warning.kind.value, "message": warning.message} if warning is not None else None
    )
    return {"clients": clients, "cloud_sync_warning": cloud_sync_warning}
