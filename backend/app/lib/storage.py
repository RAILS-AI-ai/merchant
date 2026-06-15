from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings


def images_dir() -> Path:
    root = Path(get_settings().storage_root) / "images"
    root.mkdir(parents=True, exist_ok=True)
    return root


def image_url(key: str) -> str:
    base = get_settings().images_url.rstrip("/")
    return f"{base}/{key}"


def save_image(key: str, data: bytes) -> str:
    path = images_dir() / key
    path.write_bytes(data)
    return image_url(key)


def read_image(key: str) -> bytes | None:
    path = images_dir() / key
    if not path.exists():
        return None
    return path.read_bytes()


def delete_image(key: str) -> bool:
    path = images_dir() / key
    if path.exists():
        path.unlink()
        return True
    return False
