from typing import Literal

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from app.deps.auth import require_admin
from app.domain.errors import ApiError
from app.domain.utils import uuid4
from app.lib.storage import delete_image, read_image, save_image

router = APIRouter(prefix="/v1/images", tags=["Images"])

MAX_FILE_SIZE = 5 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}
EXTENSION_CONTENT_TYPES = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


class ImageUploadOut(BaseModel):
    url: str
    key: str


class OkOut(BaseModel):
    ok: Literal[True] = True


def _validate_key(key: str) -> None:
    if not key or ".." in key or "/" in key or "\\" in key:
        raise ApiError.invalid_request("Invalid image key")


@router.post("", response_model=ImageUploadOut)
async def upload_image(
    file: UploadFile = File(...),
    _auth=Depends(require_admin),
) -> ImageUploadOut:
    if not file.content_type or file.content_type not in ALLOWED_CONTENT_TYPES:
        raise ApiError.invalid_request("File must be jpeg, png, webp, or gif")

    data = await file.read()
    if len(data) > MAX_FILE_SIZE:
        raise ApiError.invalid_request("File must be under 5MB")

    ext = ALLOWED_CONTENT_TYPES[file.content_type]
    key = f"{uuid4()}.{ext}"
    url = save_image(key, data)
    return ImageUploadOut(url=url, key=key)


@router.get("/{key}")
def get_image(key: str) -> Response:
    _validate_key(key)
    data = read_image(key)
    if data is None:
        raise ApiError.not_found("Image not found")

    ext = key.rsplit(".", 1)[-1].lower()
    content_type = EXTENSION_CONTENT_TYPES.get(ext, "image/jpeg")
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=31536000"},
    )


@router.delete("/{key}", response_model=OkOut)
def remove_image(
    key: str,
    _auth=Depends(require_admin),
) -> OkOut:
    _validate_key(key)
    if not key:
        raise ApiError.invalid_request("Image key is required")

    delete_image(key)
    return OkOut()
