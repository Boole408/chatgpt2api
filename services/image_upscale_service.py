from __future__ import annotations

import io
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageOps

from services.image_storage_service import image_storage_service
from utils.log import logger


@dataclass(frozen=True)
class UpscaleSource:
    path: str
    url: str


class ImageUpscaleError(RuntimeError):
    def __init__(self, message: str, *, source: UpscaleSource | None = None, status_code: int = 502) -> None:
        super().__init__(message)
        self.source = source
        self.status_code = status_code
        self.code = "upscale_timeout" if status_code == 504 else "upscale_failed"


def _target_dimensions(target: str) -> tuple[int, int]:
    width, height = (int(part) for part in target.split("x", 1))
    if min(width, height) <= 0 or width * height > 16_000_000:
        raise ValueError("unsupported upscale target")
    return width, height


def upscale_image_bytes(
    image_data: bytes,
    target: str,
    base_url: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
    source_callback: Callable[[UpscaleSource], None] | None = None,
    *,
    saved_source: UpscaleSource | None = None,
) -> tuple[bytes, UpscaleSource]:
    """Store the original, then request exact-size CPU super-resolution."""
    target_size = _target_dimensions(target)
    source = saved_source
    if source is None:
        stored = image_storage_service.save(image_data, base_url)
        source = UpscaleSource(path=stored.rel, url=stored.url)
    if source_callback:
        source_callback(source)
    try:
        with Image.open(io.BytesIO(image_data)) as raw:
            oriented = ImageOps.exif_transpose(raw)
            oriented.load()
            source_size = oriented.size
            ratio_loss = 1 - min(
                source_size[0] / source_size[1], target_size[0] / target_size[1]
            ) / max(source_size[0] / source_size[1], target_size[0] / target_size[1])
            if ratio_loss > 0.02:
                raise ImageUpscaleError("原图宽高比与目标尺寸不匹配，无法安全超分", source=source)
            if source_size[0] * 2 < target_size[0] or source_size[1] * 2 < target_size[1]:
                raise ImageUpscaleError("原图像素不足以通过一次 2 倍超分达到目标尺寸", source=source)
            normalized = io.BytesIO()
            oriented.save(normalized, format="PNG")
    except ImageUpscaleError:
        raise
    except Exception as exc:
        raise ImageUpscaleError("原图无法解码，超分失败", source=source) from exc

    worker_url = os.environ.get("IMAGE_UPSCALE_WORKER_URL", "").strip().rstrip("/")
    if not worker_url:
        raise ImageUpscaleError("超分服务未配置", source=source, status_code=503)
    if progress_callback:
        progress_callback("upscaling")
    started = time.monotonic()
    request = urllib.request.Request(
        f"{worker_url}/upscale",
        data=normalized.getvalue(),
        headers={"Content-Type": "image/png", "X-Target-Size": target},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5400) as response:
            output = response.read()
        with Image.open(io.BytesIO(output)) as result:
            if result.size != target_size:
                raise ImageUpscaleError("超分服务返回了错误的图片尺寸", source=source)
            result.verify()
        logger.info({"event": "image_upscale_timing", "target": target,
                     "duration_ms": int((time.monotonic() - started) * 1000)})
        return output, source
    except ImageUpscaleError:
        raise
    except urllib.error.HTTPError as exc:
        status = 504 if exc.code == 504 else (503 if exc.code == 503 else 502)
        raise ImageUpscaleError("超分服务处理失败", source=source, status_code=status) from exc
    except TimeoutError as exc:
        raise ImageUpscaleError("超分处理超时", source=source, status_code=504) from exc
    except Exception as exc:
        raise ImageUpscaleError("超分服务不可用或结果无效", source=source) from exc
