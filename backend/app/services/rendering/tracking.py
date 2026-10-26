"""Follow the talking person through a clip so the 9:16 crop stays on them.

Faces are found with the Haar cascade that ships inside opencv-python-headless. FFmpeg decodes
a small grayscale copy of the clip a few times per second, so OpenCV never has to open the
video itself. The largest face wins, with a bonus for the person already being followed so two
faces of similar size don't make the crop flip back and forth. The resulting centre track is
smoothed, rate-limited and reduced to a few keyframes that crop.py turns into an FFmpeg expression.

Tracking is best effort: if decoding fails or no face is found, the caller falls back to a
centred crop instead of failing the render.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import cv2
import numpy as np

from app.services.rendering.ffmpeg import MediaInfo, ffmpeg_bin

log = logging.getLogger("clipforge.tracking")

SAMPLE_FPS = 4  # analysis frames per second
ANALYSIS_WIDTH = 640  # frames are scaled to this width before detection
MIN_FACE_FRAC = 0.08  # smallest face side, as a fraction of frame width
SAME_PERSON_FRAC = 0.15  # faces this close to the last subject count as the same person
SAME_PERSON_BONUS = 1.5  # score multiplier for the subject already being followed
MEDIAN_WINDOW = 5  # samples; removes single-frame false detections
MAX_PAN_PER_SEC = 0.30  # largest camera move, as a fraction of frame width per second
MAX_KEYFRAMES = 60  # keeps the FFmpeg expression short enough to evaluate per frame
SIMPLIFY_TOL = 0.004  # drop keyframes that sit this close to a straight line

Keyframe = tuple[float, float]  # (seconds from clip start, subject centre as 0..1 of frame width)


def subject_keyframes(source: Path, info: MediaInfo, start: float, duration: float) -> list[Keyframe] | None:
    """Return the subject's horizontal centre across the clip, or None if no face was found."""
    size = info.display_size
    if not size or duration <= 0:
        return None
    src_w, src_h = size
    width = ANALYSIS_WIDTH
    height = max(2, round(width * src_h / src_w / 2) * 2)  # even height for the gray frames

    try:
        centres = _sample_centres(source, start, duration, width, height)
    except (OSError, subprocess.SubprocessError, cv2.error) as exc:
        log.warning("Motivspårning misslyckades, använder centrerad beskärning: %s", exc)
        return None

    known = [c for c in centres if c is not None]
    if not known:
        log.info("Inget ansikte hittades i klippet, använder centrerad beskärning.")
        return None

    # Hold the last known position across gaps. Before the first detection, use the first one.
    filled = np.empty(len(centres), dtype=np.float64)
    last = known[0]
    for i, c in enumerate(centres):
        if c is not None:
            last = c
        filled[i] = last

    sample_t = np.arange(len(filled)) / SAMPLE_FPS
    smoothed = _rate_limit(_median(filled, MEDIAN_WINDOW), MAX_PAN_PER_SEC / SAMPLE_FPS)

    step = max(1.0, duration / MAX_KEYFRAMES)
    times = np.unique(np.append(np.arange(0.0, duration, step), duration))
    values = np.interp(times, sample_t, smoothed)
    keys = [(round(float(t), 3), round(float(v), 4)) for t, v in zip(times, values, strict=True)]
    return _simplify(keys)


def _sample_centres(
    source: Path, start: float, duration: float, width: int, height: int
) -> list[float | None]:
    cascade = Path(cv2.__file__).parent / "data" / "haarcascade_frontalface_default.xml"
    detector = cv2.CascadeClassifier(str(cascade))
    if detector.empty():
        raise cv2.error("Ansiktsmodellen kunde inte läsas")
    centres: list[float | None] = []
    previous: float | None = None
    for gray in _gray_frames(source, start, duration, width, height):
        cx = _subject_centre(detector, gray, previous)
        if cx is not None:
            previous = cx
        centres.append(cx)
    return centres


def _gray_frames(source: Path, start: float, duration: float, width: int, height: int):
    """Yield grayscale analysis frames for the clip range, decoded by FFmpeg at low resolution."""
    cmd = [
        ffmpeg_bin(),
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-vf",
        f"fps={SAMPLE_FPS},scale={width}:{height}:flags=area",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    frame_bytes = width * height
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        assert proc.stdout is not None
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width).copy()
        proc.wait()


def _subject_centre(
    detector: cv2.CascadeClassifier, gray: np.ndarray, previous: float | None
) -> float | None:
    """Return the centre of the most prominent face in a frame, as 0..1 of frame width."""
    frame_h, frame_w = gray.shape
    min_side = max(24, int(frame_w * MIN_FACE_FRAC))
    faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(min_side, min_side))
    best_cx: float | None = None
    best_score = 0.0
    for x, _y, w, h in faces:
        cx = (x + w / 2) / frame_w
        score = float(w * h)
        if previous is not None and abs(cx - previous) < SAME_PERSON_FRAC:
            score *= SAME_PERSON_BONUS
        if score > best_score:
            best_cx, best_score = float(cx), score
    return best_cx


def _median(values: np.ndarray, window: int) -> np.ndarray:
    if len(values) < window:
        return values
    padded = np.pad(values, window // 2, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, window)
    return np.median(windows, axis=1)


def _rate_limit(values: np.ndarray, max_step: float) -> np.ndarray:
    """Move towards each target value by at most max_step per sample, like a steady camera pan."""
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = out[i - 1] + np.clip(values[i] - out[i - 1], -max_step, max_step)
    return out


def _simplify(keys: list[Keyframe]) -> list[Keyframe]:
    """Drop keyframes that lie on the straight line between their neighbours (within tolerance)."""
    if len(keys) <= 2:
        return keys
    kept = [keys[0]]
    for i in range(1, len(keys) - 1):
        t0, v0 = kept[-1]
        t, v = keys[i]
        t1, v1 = keys[i + 1]
        predicted = v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        if abs(v - predicted) > SIMPLIFY_TOL:
            kept.append(keys[i])
    kept.append(keys[-1])
    return kept
