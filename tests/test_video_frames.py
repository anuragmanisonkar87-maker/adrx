"""Frames for on-screen text: only keyframes are decoded when they come often enough (long and 4K videos), every
frame otherwise, and a video that takes too long to read still gets checked with the frames read so far."""

import dataclasses
import logging
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.config import Settings
from app.extract import media
from tests.conftest import has_ffmpeg, has_tesseract

pytestmark = pytest.mark.skipif(not (has_ffmpeg() and has_tesseract()), reason="needs ffmpeg and tesseract")

CAPTIONS = ["MUMBAI NO.1 SURGEON", "FLAT 40% OFF TODAY", "BOOK YOUR SLOT NOW", "PAINLESS CURE GUARANTEED"]
SECONDS_EACH = 10  # each caption is shown for 10 s: a 40 s video


def _settings() -> Settings:
    return dataclasses.replace(Settings.from_env(), max_video_frames=8, max_media_seconds=120,
                               ocr_frame_side=1280, ocr_frame_workers=1, ffmpeg_threads=0)


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=180)


@pytest.fixture(scope="module")
def videos(tmp_path_factory) -> dict[str, Path]:
    """The same 40 s video twice: a keyframe every 2 s (like phone cameras), and a single keyframe."""
    d = tmp_path_factory.mktemp("frames")
    font = ImageFont.load_default(size=44)
    inputs, labels = [], []
    for i, caption in enumerate(CAPTIONS):
        img = Image.new("RGB", (640, 360), "white")
        ImageDraw.Draw(img).text((30, 150), caption, fill="black", font=font)
        img.save(d / f"slide{i}.png")
        inputs += ["-loop", "1", "-framerate", "5", "-t", str(SECONDS_EACH), "-i", str(d / f"slide{i}.png")]
        labels.append(f"[{i}:v]")
    joined = "".join(labels) + f"concat=n={len(CAPTIONS)}:v=1:a=0[v]"
    out = {}
    for name, gop in (("frequent_keyframes.mp4", "10"), ("one_keyframe.mp4", "1000")):
        _ffmpeg(*inputs, "-filter_complex", joined, "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-g", gop, "-keyint_min", gop, "-sc_threshold", "0", str(d / name))
        out[name] = d / name
    return out


def _read(path: Path, settings: Settings | None = None) -> media.MediaText:
    return media.extract_media(path, settings or _settings(), "en")


def _shows_all_captions(result: media.MediaText) -> bool:
    words = set(" ".join(result.onscreen_lines).upper().split())
    return all(any(w in words for w in caption.split() if len(w) > 3) for caption in CAPTIONS)


def test_keyframes_suffice_only_when_they_come_as_often_as_the_samples():
    every_2s = [float(t) for t in range(0, 120, 2)]
    assert media._keyframes_suffice(0.0, every_2s, 120.0, 15.0)
    assert media._keyframes_suffice(1.4, [t + 1.4 for t in every_2s], 120.0, 15.0)  # TV streams start late
    assert not media._keyframes_suffice(0.0, [0.0], 120.0, 15.0)  # one keyframe: decode everything
    assert not media._keyframes_suffice(0.0, [0.0, 6.5, 13.08], 19.6, 2.45)  # a short reel with few keyframes
    assert not media._keyframes_suffice(0.0, [0.0, 8.0, 16.0], 40.0, 5.0)  # nothing after 16 s


def test_frequent_keyframes_are_read_alone(videos, caplog):
    with caplog.at_level(logging.INFO, logger="app.extract.media"):
        result = _read(videos["frequent_keyframes.mp4"])
    assert "(keyframes)" in caplog.text
    assert _shows_all_captions(result), result.onscreen_lines
    assert not result.warnings


def test_sparse_keyframes_decode_every_frame(videos, caplog):
    with caplog.at_level(logging.INFO, logger="app.extract.media"):
        result = _read(videos["one_keyframe.mp4"])
    assert "(all frames)" in caplog.text
    assert _shows_all_captions(result), result.onscreen_lines


def test_decoder_without_keyframe_reading_falls_back(videos, monkeypatch, caplog):
    real_run = media._run

    def run(cmd, timeout=600):
        if "-skip_frame" in cmd:
            raise media.MediaError("Could not read the media file (unsupported)")
        return real_run(cmd, timeout)

    monkeypatch.setattr(media, "_run", run)
    with caplog.at_level(logging.INFO, logger="app.extract.media"):
        result = _read(videos["frequent_keyframes.mp4"])
    assert "(all frames)" in caplog.text
    assert _shows_all_captions(result), result.onscreen_lines


def test_running_out_of_time_keeps_the_frames_read_so_far(videos, monkeypatch):
    real_run = media._run

    def run(cmd, timeout=600):
        out = real_run(cmd, timeout)
        if cmd[0] == "ffmpeg" and cmd[-1].endswith("frame_%03d.jpg"):
            target = Path(cmd[-1]).parent
            first = (target / "frame_001.jpg").read_bytes()
            (target / "frame_099.jpg").write_bytes(first[: len(first) // 3])  # cut off mid-write
            raise media.MediaTimeout("Media processing timed out")
        return out

    monkeypatch.setattr(media, "_run", run)
    result = _read(videos["one_keyframe.mp4"])
    assert any("took too long" in w for w in result.warnings)
    assert result.frames_read >= 4 and _shows_all_captions(result), result.onscreen_lines


def test_ffmpeg_threads_setting_is_passed_on(videos, monkeypatch):
    commands = []
    real_run = media._run

    def run(cmd, timeout=600):
        commands.append(cmd)
        return real_run(cmd, timeout)

    monkeypatch.setattr(media, "_run", run)
    _read(videos["frequent_keyframes.mp4"], dataclasses.replace(_settings(), ffmpeg_threads=1))
    frames_cmd = next(c for c in commands if c[0] == "ffmpeg" and c[-1].endswith("frame_%03d.jpg"))
    assert frames_cmd[frames_cmd.index("-threads") + 1] == "1"
    assert frames_cmd.index("-threads") < frames_cmd.index("-i")  # a decoder option: before the input
