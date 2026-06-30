"""Photo editing helpers for Telegram images."""

from __future__ import annotations

import base64
import io
import os
from typing import Any

import requests
from PIL import Image, ImageOps


class PhotoEditError(RuntimeError):
    """Raised when the image edit API cannot produce an edited image."""


IMAGE_EDIT_URL = os.environ.get("OPENAI_IMAGE_EDIT_URL", "https://api.openai.com/v1/images/edits")
DEFAULT_IMAGE_MODEL = os.environ.get("PHOTO_EDITOR_MODEL", "gpt-image-1")
DEFAULT_IMAGE_SIZE = os.environ.get("PHOTO_EDITOR_SIZE", "").strip()
DEFAULT_MAX_SIDE = int(os.environ.get("PHOTO_EDITOR_MAX_SIDE", "1536"))
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("PHOTO_EDITOR_TIMEOUT_SECONDS", "180"))


def image_editor_available(api_key: str | None = None) -> bool:
    return bool((api_key or os.environ.get("OPENAI_API_KEY") or "").strip())


def prepare_image_for_edit(image_bytes: bytes, *, max_side: int = DEFAULT_MAX_SIDE) -> bytes:
    """Normalize Telegram photo bytes into a compact PNG accepted by image-edit APIs."""
    if not image_bytes:
        raise PhotoEditError("Пустое фото.")

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image = ImageOps.exif_transpose(image)
    except Exception as e:
        raise PhotoEditError(f"Не смог прочитать фото: {e}") from e

    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")

    if max_side > 0:
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def edit_photo(
    image_bytes: bytes,
    prompt: str,
    *,
    api_key: str | None = None,
    model: str = DEFAULT_IMAGE_MODEL,
    size: str = DEFAULT_IMAGE_SIZE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Edit one image and return PNG/JPEG bytes from OpenAI Images API."""
    key = (api_key or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise PhotoEditError("Нет OPENAI_API_KEY.")

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        clean_prompt = "Улучши фото естественно: свет, резкость, цвета. Не меняй смысл сцены."

    png_bytes = prepare_image_for_edit(image_bytes)
    last_error: PhotoEditError | None = None
    for image_field in ("image[]", "image"):
        try:
            return _request_image_edit(
                png_bytes,
                clean_prompt,
                api_key=key,
                model=model,
                size=size,
                timeout_seconds=timeout_seconds,
                image_field=image_field,
            )
        except PhotoEditError as e:
            last_error = e
            msg = str(e).lower()
            if image_field == "image[]" and (
                "image[]" in msg
                or "missing required parameter" in msg
                or "unknown parameter" in msg
                or "invalid type" in msg
            ):
                continue
            raise
    raise last_error or PhotoEditError("Неизвестная ошибка фоторедактора.")


def _request_image_edit(
    png_bytes: bytes,
    prompt: str,
    *,
    api_key: str,
    model: str,
    size: str,
    timeout_seconds: int,
    image_field: str,
) -> bytes:
    data: dict[str, str] = {
        "model": model,
        "prompt": prompt,
    }
    if size:
        data["size"] = size

    files = [
        (image_field, ("photo.png", png_bytes, "image/png")),
    ]
    try:
        resp = requests.post(
            IMAGE_EDIT_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            data=data,
            files=files,
            timeout=timeout_seconds,
        )
    except requests.exceptions.Timeout as e:
        raise PhotoEditError("OpenAI долго не отвечает. Попробуй ещё раз или фото попроще.") from e
    except requests.RequestException as e:
        raise PhotoEditError(f"Ошибка сети при редактировании фото: {e}") from e

    if resp.status_code >= 400:
        raise PhotoEditError(_api_error_message(resp))

    try:
        payload = resp.json()
        item = payload["data"][0]
    except Exception as e:
        raise PhotoEditError(f"Неожиданный ответ OpenAI: {resp.text[:200]}") from e

    image_data = _image_bytes_from_item(item)
    if not image_data:
        raise PhotoEditError("OpenAI не вернул картинку.")
    return image_data


def _image_bytes_from_item(item: dict[str, Any]) -> bytes:
    b64 = item.get("b64_json")
    if b64:
        return base64.b64decode(b64)

    url = item.get("url")
    if url:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    return b""


def _api_error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict):
            message = err.get("message") or str(err)
        else:
            message = str(data)
    except Exception:
        message = resp.text
    return f"OpenAI вернул ошибку {resp.status_code}: {message[:500]}"
