"""Video filter construction for 9:16 output."""

from __future__ import annotations


def video_filter(
    mode: str,
    out_w: int,
    out_h: int,
    fps: int,
    crop_offset: float | None,
    track: list[tuple[float, float]] | None = None,
) -> str:
    """Return a filtergraph fragment from ``[0:v]`` to ``[base]``.

    center_crop: crop the largest 9:16 window and scale. The window follows ``track`` when given
                 (keyframes of ``(seconds, subject centre 0..1)``), otherwise it sits at the manual
                 horizontal offset ``crop_offset`` (0..1, default centred).
    fit_blur:    whole frame fitted on top of a blurred, zoomed copy of itself (no content cut off).
    """
    off = 0.5 if crop_offset is None else min(1.0, max(0.0, float(crop_offset)))
    if mode == "fit_blur":
        bw, bh = out_w // 4, out_h // 4
        return (
            f"[0:v]fps={fps},split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
            f"boxblur=10:2,scale={out_w}:{out_h},setsar=1[bg];"
            f"[fgsrc]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base]"
        )
    if track:
        # Centre the window on the subject, clamped so it never leaves the frame.
        x = f"max(0\\,min(iw-ow\\,({_centre_expr(track)})*iw-ow/2))"
    else:
        x = f"(iw-ow)*{off:.4f}"
    # Ratio 9:16 window; even dimensions for yuv420p.
    return (
        f"[0:v]fps={fps},"
        f"crop=w='trunc(min(iw\\,ih*9/16)/2)*2':h='trunc(min(ih\\,iw*16/9)/2)*2':"
        f"x='{x}':y='(ih-oh)/2',"
        f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[base]"
    )


def _centre_expr(track: list[tuple[float, float]]) -> str:
    """Nested if() expression that linearly interpolates the subject centre over clip time ``t``.

    Times before the first keyframe and after the last one hold the nearest value. ffmpeg
    re-evaluates the crop expressions for every frame, so ``t`` follows the clip timeline.
    """
    expr = f"{track[-1][1]:.4f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(track, track[1:], strict=False))):
        span = t1 - t0
        lerp = f"({v0:.4f}{v1 - v0:+.4f}*(t-{t0:.3f})/{span:.3f})"
        expr = f"if(lt(t\\,{t1:.3f})\\,{lerp}\\,{expr})"
    return expr
