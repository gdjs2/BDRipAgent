"""Optional CUDA decoding with bounded, seekable screenshot windows."""

from contextlib import contextmanager
from itertools import chain

import av


def start_decoder(path, *, cuda=False):
    options = {}
    if cuda:
        from av.codec.hwaccel import HWAccel

        # Probe explicitly so the UI never reports GPU while silently using CPU.
        options["hwaccel"] = HWAccel("cuda", allow_software_fallback=False)
    container = av.open(str(path), **options)
    try:
        frames = container.decode(video=0)
        first = next(frames)
        if cuda and not container.streams.video[0].codec_context.is_hwaccel:
            raise RuntimeError("The source codec did not activate GPU decoding")
        return container, chain((first,), frames)
    except BaseException:
        container.close()
        raise


@contextmanager
def candidate_decoder(ctx):
    requested = ctx.job.screenshot_policy.get("decoder", "cuda")
    if requested not in ("cpu", "cuda"):
        raise ValueError(f"Unknown screenshot decoder: {requested}")
    source = ctx.source()
    info = {"decoder_requested": requested, "decoder": "cpu", "decoder_fallback": False}
    if requested == "cuda":
        ctx.check()
        try:
            container, frames = start_decoder(source, cuda=True)
        except (ImportError, OSError, RuntimeError, ValueError, StopIteration) as error:
            ctx.check()
            reason = f"{type(error).__name__}: {error}"[:500]
            ctx.log(f"GPU screenshot decoding unavailable; falling back to CPU. {reason}")
            info.update(decoder_fallback=True, decoder_fallback_reason=reason)
            container, frames = start_decoder(source)
        else:
            info["decoder"] = "cuda"
    else:
        container, frames = start_decoder(source)
    try:
        ctx.log(
            "Screenshot scan decoder: " + ("NVIDIA GPU (CUDA/NVDEC)" if info["decoder"] == "cuda" else "CPU")
        )
        yield container, frames, info
    finally:
        container.close()


@contextmanager
def candidate_frames(ctx):
    with candidate_decoder(ctx) as (_, frames, info):
        yield frames, info


def window_frames(ctx, container, start, end, stats):
    """Seek to the previous keyframe; decode only its preroll and this window."""
    stream = container.streams.video[0]
    container.seek(int(start / stream.time_base), stream=stream, backward=True)
    for frame in container.decode(stream):
        ctx.check()
        stats["decoded_frames"] += 1
        if frame.pts is None:
            raise ValueError("Source candidate has no PTS")
        pts = float(frame.pts * frame.time_base)
        if pts > end:
            break
        if pts >= start:
            yield frame
