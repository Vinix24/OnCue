"""Upload routes for hub client assets."""

from __future__ import annotations

import stat
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from sales_copilot.core.context_docs import UPLOAD_ROOT
from sales_copilot.websocket.hub_auth import require_token

router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".txt", ".json", ".md"}


def _slugify(value: str) -> str:
    cleaned = "".join(char.lower() if char.isalnum() else "-" for char in value.strip())
    slug = "-".join(part for part in cleaned.split("-") if part)
    return slug or "default"


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

    company_slug = _slugify(company_slug)
    target_dir = UPLOAD_ROOT / company_slug
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir.chmod(stat.S_IRWXU)
    target_path = target_dir / f"{uuid.uuid4().hex}{ext}"
    target_path.write_bytes(file_bytes)
    target_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    return {"id": target_path.relative_to(UPLOAD_ROOT).as_posix(), "name": filename}
