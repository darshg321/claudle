"""Screen capture, screen recording and webcam capture.

Every function here is blocking; call them through asyncio.to_thread.
"""
from __future__ import annotations

import io
import time

from PIL import Image

import config

_GIF_MAX_WIDTH = 900


def _encode(img: Image.Image, prefer_png: bool = True) -> tuple[bytes, str]:
    """Encode an image, shrinking/recompressing until it fits the upload cap."""
    limit = config.MAX_UPLOAD_BYTES

    if prefer_png:
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        if buf.tell() <= limit:
            return buf.getvalue(), "png"

    work = img.convert("RGB")
    quality = 85
    for _ in range(12):
        buf = io.BytesIO()
        work.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= limit:
            return buf.getvalue(), "jpg"
        if quality > 40:
            quality -= 15
        else:
            new_size = (max(640, int(work.width * 0.7)), max(360, int(work.height * 0.7)))
            if new_size == work.size:
                break
            work = work.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    work.save(buf, "JPEG", quality=35, optimize=True)
    return buf.getvalue(), "jpg"


def _primary(sct) -> dict:
    """The primary display — the only screen this module ever captures.

    Newer mss tags monitors with is_primary. Older versions don't, so fall
    back to the top-left corner, which Windows pins to (0, 0) on the primary.
    """
    for mon in sct.monitors[1:]:
        if mon.get("is_primary"):
            return mon
    for mon in sct.monitors[1:]:
        if mon["left"] == 0 and mon["top"] == 0:
            return mon
    return sct.monitors[1]


def primary_monitor() -> dict:
    """Geometry of the primary display."""
    import mss

    with mss.mss() as sct:
        return dict(_primary(sct))


def _grab_raw() -> Image.Image:
    import mss

    with mss.mss() as sct:
        shot = sct.grab(_primary(sct))
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def screenshot(scale: float = 1.0) -> tuple[bytes, str, tuple[int, int]]:
    """Capture the primary monitor."""
    img = _grab_raw()
    full_size = img.size
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    data, ext = _encode(img)
    return data, ext, full_size


def record_gif(seconds: float, fps: float = 4.0) -> tuple[bytes, int]:
    """Record the primary monitor to an animated GIF. Returns (bytes, frame_count)."""
    import mss

    seconds = max(1.0, min(seconds, 60.0))
    fps = max(1.0, min(fps, 10.0))
    interval = 1.0 / fps
    frames: list[Image.Image] = []
    deadline = time.monotonic() + seconds

    with mss.mss() as sct:
        region = _primary(sct)
        while time.monotonic() < deadline:
            tick = time.monotonic()
            shot = sct.grab(region)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            if img.width > _GIF_MAX_WIDTH:
                ratio = _GIF_MAX_WIDTH / img.width
                img = img.resize((_GIF_MAX_WIDTH, int(img.height * ratio)), Image.LANCZOS)
            frames.append(img.convert("P", palette=Image.ADAPTIVE, colors=128))
            sleep_for = interval - (time.monotonic() - tick)
            if sleep_for > 0:
                time.sleep(sleep_for)

    if not frames:
        raise RuntimeError("captured no frames")

    for attempt in range(4):
        buf = io.BytesIO()
        frames[0].save(
            buf,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            duration=int(interval * 1000),
            loop=0,
            optimize=True,
        )
        if buf.tell() <= config.MAX_UPLOAD_BYTES or attempt == 3:
            return buf.getvalue(), len(frames)
        # Too big: halve the frame rate, then shrink.
        if len(frames) > 4:
            frames = frames[::2]
            interval *= 2
        else:
            frames = [
                f.convert("RGB")
                .resize((int(f.width * 0.7), int(f.height * 0.7)), Image.LANCZOS)
                .convert("P", palette=Image.ADAPTIVE, colors=96)
                for f in frames
            ]

    raise RuntimeError("could not compress recording below the upload limit")


def webcam(index: int = 0, warmup_frames: int = 8) -> tuple[bytes, str]:
    """Grab a still from a webcam."""
    import cv2

    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    try:
        if not cap.isOpened():
            raise RuntimeError(f"could not open camera {index}")
        frame = None
        for _ in range(warmup_frames):
            ok, candidate = cap.read()
            if ok:
                frame = candidate
        if frame is None:
            raise RuntimeError(f"camera {index} opened but returned no frames")
        img = Image.fromarray(frame[:, :, ::-1])  # BGR -> RGB
        return _encode(img)
    finally:
        cap.release()
