from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageOps


PROCESSING = threading.Lock()
WAITING = threading.BoundedSemaphore(4)
MAX_INPUT_BYTES = 50 * 1024 * 1024
QUEUE_WAIT_SECS = 4000
PROCESS_TIMEOUT_SECS = 1200


def process_image(source: Path, target: Path, dimensions: tuple[int, int], tile: int) -> None:
    import cv2
    import numpy as np
    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer

    torch.set_num_threads(3)
    model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=2)
    upsampler = RealESRGANer(
        scale=2,
        model_path=os.environ.get("UPSCALE_MODEL_PATH", "/models/RealESRGAN_x2plus.pth"),
        model=model,
        tile=tile,
        tile_pad=10,
        pre_pad=0,
        half=False,
        device=torch.device("cpu"),
    )
    image = cv2.imdecode(np.frombuffer(source.read_bytes(), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("invalid source image")
    enlarged, _ = upsampler.enhance(image, outscale=2)
    if enlarged.ndim == 2:
        pil_image = Image.fromarray(enlarged)
    elif enlarged.shape[2] == 4:
        pil_image = Image.fromarray(cv2.cvtColor(enlarged, cv2.COLOR_BGRA2RGBA))
    else:
        pil_image = Image.fromarray(cv2.cvtColor(enlarged, cv2.COLOR_BGR2RGB))
    if pil_image.width < dimensions[0] or pil_image.height < dimensions[1]:
        raise ValueError("upscaled image is smaller than requested target")
    output = ImageOps.fit(pil_image, dimensions, method=Image.Resampling.LANCZOS)
    output.save(target, format="PNG")


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: bytes = b"", content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._reply(200, b"ok")
        else:
            self._reply(404)

    def do_POST(self) -> None:
        if self.path != "/upscale":
            self._reply(404)
            return
        match = re.fullmatch(r"(\d{1,5})x(\d{1,5})", self.headers.get("X-Target-Size", ""))
        dimensions = tuple(map(int, match.groups())) if match else None
        length = int(self.headers.get("Content-Length", "0") or 0)
        valid_target = bool(dimensions and min(dimensions) >= 1024
                            and max(dimensions) <= 3840
                            and dimensions[0] * dimensions[1] <= 16_000_000)
        if not valid_target or length <= 0 or length > MAX_INPUT_BYTES:
            self._reply(400, b"invalid size or image length")
            return
        if not WAITING.acquire(blocking=False):
            self._reply(503, b"upscale queue full")
            return
        try:
            if not PROCESSING.acquire(timeout=QUEUE_WAIT_SECS):
                self._reply(504, b"upscale queue timeout")
                return
            try:
                payload = self.rfile.read(length)
                with tempfile.TemporaryDirectory(prefix="upscale-") as temporary:
                    input_path = Path(temporary) / "input.png"
                    output_path = Path(temporary) / "output.png"
                    input_path.write_bytes(payload)
                    for tile in (256, 128):
                        try:
                            result = subprocess.run(
                                [sys.executable, __file__, "--process", str(input_path), str(output_path),
                                 str(dimensions[0]), str(dimensions[1]), str(tile)],
                                capture_output=True, timeout=PROCESS_TIMEOUT_SECS, check=False,
                            )
                        except subprocess.TimeoutExpired:
                            self._reply(504, b"upscale processing timeout")
                            return
                        if result.returncode == 0 and output_path.is_file():
                            self._reply(200, output_path.read_bytes(), "image/png")
                            return
                        if tile == 256 and (result.returncode in {-9, 137, 42} or b"out of memory" in result.stderr.lower()):
                            continue
                        break
                    self._reply(502, b"upscale processing failed")
            finally:
                PROCESSING.release()
        finally:
            WAITING.release()


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--process":
        process_image(Path(sys.argv[2]), Path(sys.argv[3]), (int(sys.argv[4]), int(sys.argv[5])), int(sys.argv[6]))
    else:
        ThreadingHTTPServer(("0.0.0.0", 8091), Handler).serve_forever()
