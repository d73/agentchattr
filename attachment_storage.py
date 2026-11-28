"""Image upload policy shared by the web and MCP entry points."""

from pathlib import Path
from uuid import uuid4

IMAGE_UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
DEFAULT_IMAGE_LIMIT_MB = 10
COPY_CHUNK_BYTES = 64 * 1024


def image_upload_limit(config: dict) -> tuple[float, int]:
    max_mb = config.get("images", {}).get("max_size_mb", DEFAULT_IMAGE_LIMIT_MB)
    return max_mb, int(max_mb * 1024 * 1024)


def copy_image_attachment(image_path: str, config: dict) -> tuple[dict, Path]:
    source = Path(image_path)
    if not source.is_file():
        raise ValueError(f"Image not found: {image_path}")
    if source.suffix.lower() not in IMAGE_UPLOAD_EXTS:
        raise ValueError(f"Unsupported image type: {source.suffix}")

    max_mb, max_bytes = image_upload_limit(config)
    too_large = f"file too large (max {max_mb} MB)"
    if source.stat().st_size > max_bytes:
        raise ValueError(too_large)

    upload_dir = Path(config.get("images", {}).get("upload_dir", "./uploads"))
    upload_dir.mkdir(parents=True, exist_ok=True)
    destination = upload_dir / f"{uuid4().hex}{source.suffix}"
    created = False
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            created = True
            remaining = max_bytes
            while chunk := incoming.read(min(COPY_CHUNK_BYTES, remaining + 1)):
                if len(chunk) > remaining:
                    raise ValueError(too_large)
                outgoing.write(chunk)
                remaining -= len(chunk)
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise

    return {"name": source.name, "url": f"/uploads/{destination.name}"}, destination
