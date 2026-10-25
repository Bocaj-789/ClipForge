from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RenderProfile:
    name: str
    width: int
    height: int
    fps: int
    video_args: tuple[str, ...]
    audio_bitrate: str


PROFILES: dict[str, RenderProfile] = {
    # Hardware encoder for quick previews.
    "fast_draft": RenderProfile(
        "fast_draft", 720, 1280, 30, ("-c:v", "h264_videotoolbox", "-b:v", "3500k", "-allow_sw", "1"), "128k"
    ),
    "shorts_standard": RenderProfile(
        "shorts_standard",
        1080,
        1920,
        30,
        ("-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high"),
        "160k",
    ),
    "high_quality": RenderProfile(
        "high_quality",
        1080,
        1920,
        30,
        ("-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high"),
        "192k",
    ),
}


def get_profile(name: str) -> RenderProfile:
    return PROFILES.get(name, PROFILES["shorts_standard"])
