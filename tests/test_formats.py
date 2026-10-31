"""Real files in many audio/video/image formats go through the real ffmpeg/OCR extraction.

Speech-to-text is replaced by a stand-in that checks the decoded audio (length and loudness), so these tests
prove each format is actually decoded. A representative set runs by default; the full set with:
    pytest -m formats
Formats whose encoder is missing from this computer's ffmpeg build are skipped, not failed.
"""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import extract
from app.config import Settings
from app.extract import transcribe
from tests.conftest import has_ffmpeg, has_tesseract

pytestmark = pytest.mark.skipif(not (has_ffmpeg() and has_tesseract()), reason="needs ffmpeg and tesseract")

SECONDS = 4
SPOKEN = "Book your appointment now, limited slots."
EXT = pytest.mark.formats  # extended set: pytest -m formats
SLIDE_A = ("MUMBAI", "NO.1", "SURGEON")
SLIDE_B = ("FLAT", "40%", "OFF", "TODAY", "ONLY")


def _shows(text: str, words: tuple[str, ...]) -> bool:
    """True if words of the slide were read from the picture.

    These tests check that each format is decoded, not how well OCR reads: Tesseract differs a little between
    computers (one may miss a word in a low-quality frame that another reads), so any word of the slide counts.
    How well captions are read is tested in test_ocr_frames.py.
    """
    read = set(text.upper().split())
    return any(word in read for word in words)


class CheckingWhisper:
    """Stand-in for faster-whisper that verifies it received the real decoded audio."""

    def transcribe(self, audio, **kwargs):
        import numpy as np

        seconds = len(audio) / 16000
        assert abs(seconds - SECONDS) < 0.6, f"decoded {seconds:.2f}s of audio, expected about {SECONDS}s"
        assert float(np.sqrt(np.mean(audio ** 2))) > 0.005, "decoded audio is silent"
        return iter([SimpleNamespace(text=SPOKEN)]), SimpleNamespace(language="en")


@pytest.fixture(autouse=True)
def _checking_whisper(monkeypatch):
    monkeypatch.setattr(transcribe, "_local_model", lambda s: CheckingWhisper())


def _slide(path: Path, lines: list[str]) -> Path:
    img = Image.new("RGB", (480, 480), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=58)
    for i, line in enumerate(lines):
        draw.text((30, 120 + i * 110), line, fill="black", font=font)
    img.save(path)
    return path


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True, timeout=180)


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory, samples=Path(__file__).resolve().parent.parent / "samples"):
    d = tmp_path_factory.mktemp("formats")
    _ffmpeg("-i", str(samples / "voice_note_3_english_compliant.ogg"), "-t", str(SECONDS), "-ac", "1",
            "-ar", "44100", str(d / "source.wav"))
    _slide(d / "slide_a.png", ["MUMBAI NO.1", "SURGEON"])
    _slide(d / "slide_b.png", ["FLAT 40% OFF", "TODAY ONLY"])
    return d


def _make(media_dir: Path, name: str, args: list[str], video: bool) -> Path:
    out = media_dir / name
    if out.exists():
        return out
    inputs = ["-loop", "1", "-framerate", "25", "-i", str(media_dir / "slide_a.png")] if video else []
    inputs += ["-i", str(media_dir / "source.wav")]
    maps = ["-map", "0:v", "-map", "1:a"] if video else []
    try:
        _ffmpeg(*inputs, *maps, "-t", str(SECONDS), *args, str(out))
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"this ffmpeg build cannot create {name}: {exc.stderr.decode(errors='ignore')[-160:]}")
    return out


AUDIO = [
    ("voice.mp3", ["-c:a", "libmp3lame"]),
    ("voice.m4a", ["-c:a", "aac"]),
    ("voice.wav", ["-c:a", "pcm_s16le"]),
    ("whatsapp.opus", ["-c:a", "libopus", "-ac", "1", "-b:a", "24k"]),
    ("voice.ogg", ["-c:a", "libvorbis"]),
    ("voice.wma", ["-c:a", "wmav2", "-f", "asf"]),
    ("iphone.caf", ["-c:a", "alac"]),
    ("android.3ga", ["-c:a", "aac", "-f", "3gp", "-ac", "1"]),
    pytest.param("alac.m4a", ["-c:a", "alac"], marks=EXT),
    pytest.param("voice.aac", ["-c:a", "aac", "-f", "adts"], marks=EXT),
    pytest.param("voice.flac", ["-c:a", "flac"], marks=EXT),
    pytest.param("voice.weba", ["-c:a", "libopus", "-f", "webm"], marks=EXT),
    pytest.param("voice.aiff", ["-c:a", "pcm_s16be"], marks=EXT),
    pytest.param("voice.au", ["-c:a", "pcm_mulaw"], marks=EXT),
    pytest.param("voice.ac3", ["-c:a", "ac3"], marks=EXT),
    pytest.param("voice.eac3", ["-c:a", "eac3"], marks=EXT),
    pytest.param("voice.mp2", ["-c:a", "mp2"], marks=EXT),
    pytest.param("voice.spx", ["-c:a", "libspeex", "-ar", "16000", "-ac", "1"], marks=EXT),
    pytest.param("voice.wv", ["-c:a", "wavpack"], marks=EXT),
    pytest.param("voice.tta", ["-c:a", "tta"], marks=EXT),
    pytest.param("voice.mka", ["-c:a", "libopus", "-f", "matroska"], marks=EXT),
    pytest.param("book.m4b", ["-c:a", "aac", "-f", "ipod"], marks=EXT),
    pytest.param("voice.voc", ["-c:a", "pcm_u8", "-ar", "22050"], marks=EXT),
    pytest.param("voice.w64", ["-c:a", "pcm_s16le", "-f", "w64"], marks=EXT),
]

H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
VIDEO = [
    ("clip.mp4", H264 + ["-c:a", "aac"]),
    ("iphone_hevc.mov", ["-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
                         "-x265-params", "log-level=none", "-c:a", "aac"]),
    ("clip.mkv", H264 + ["-c:a", "libopus"]),
    ("clip.webm", ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "600k",
                   "-c:a", "libopus"]),
    ("clip.avi", ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "libmp3lame"]),
    ("phone.3gp", ["-c:v", "h263", "-s", "704x576", "-c:a", "aac", "-ar", "16000", "-ac", "1"]),
    ("clip.wmv", ["-c:v", "wmv2", "-q:v", "3", "-c:a", "wmav2"]),
    ("tv.ts", H264 + ["-c:a", "aac", "-f", "mpegts"]),
    pytest.param("clip_vp8.webm", ["-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "600k",
                                   "-c:a", "libvorbis"], marks=EXT),
    pytest.param("clip.flv", ["-c:v", "flv1", "-q:v", "3", "-c:a", "libmp3lame", "-ar", "44100"], marks=EXT),
    pytest.param("cam.m2ts", H264 + ["-c:a", "aac", "-f", "mpegts"], marks=EXT),
    pytest.param("clip.mpg", ["-c:v", "mpeg2video", "-q:v", "3", "-c:a", "mp2"], marks=EXT),
    pytest.param("dvd.vob", ["-c:v", "mpeg2video", "-q:v", "3", "-c:a", "ac3", "-f", "vob"], marks=EXT),
    pytest.param("clip.ogv", ["-c:v", "libtheora", "-q:v", "7", "-c:a", "libvorbis"], marks=EXT),
    pytest.param("camcorder.dv", ["-c:v", "dvvideo", "-s", "720x576", "-r", "25", "-pix_fmt", "yuv420p",
                                  "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"], marks=EXT),
    pytest.param("broadcast.mxf", ["-c:v", "mpeg2video", "-q:v", "3", "-r", "25", "-c:a", "pcm_s16le",
                                   "-ar", "48000"], marks=EXT),
    pytest.param("clip.nut", H264 + ["-c:a", "aac"], marks=EXT),
    pytest.param("clip.f4v", H264 + ["-c:a", "aac"], marks=EXT),
    pytest.param("clip.m4v", H264 + ["-c:a", "aac"], marks=EXT),
    pytest.param("clip.asf", ["-c:v", "wmv2", "-q:v", "3", "-c:a", "wmav2"], marks=EXT),
    pytest.param("clip_av1.mkv", ["-c:v", "libsvtav1", "-preset", "12", "-pix_fmt", "yuv420p",
                                  "-c:a", "libopus"], marks=EXT),
    pytest.param("edit_prores.mov", ["-c:v", "prores", "-profile:v", "0", "-pix_fmt", "yuv422p10le",
                                     "-c:a", "pcm_s16le"], marks=EXT),
    pytest.param("clip_mjpeg.avi", ["-c:v", "mjpeg", "-q:v", "4", "-c:a", "pcm_s16le"], marks=EXT),
]


@pytest.mark.parametrize("name, args", AUDIO)
def test_audio_formats(media_dir, name, args):
    path = _make(media_dir, name, args, video=False)
    result = extract.from_file(path, name, Settings.from_env())
    assert result.kind == "audio" and result.text == SPOKEN


@pytest.mark.parametrize("name, args", VIDEO)
def test_video_formats(media_dir, name, args):
    path = _make(media_dir, name, args, video=True)
    result = extract.from_file(path, name, Settings.from_env())
    assert result.kind == "video"
    assert SPOKEN in result.text, "voice-over was not transcribed"
    assert result.meta["frames_read"] >= 1 and _shows(result.text, SLIDE_A), result.text


def test_video_without_sound(media_dir):
    path = _make(media_dir, "silent.mp4", ["-an"] + H264, video=True)
    result = extract.from_file(path, "silent.mp4", Settings.from_env())
    assert result.kind == "video" and _shows(result.text, SLIDE_A) and SPOKEN not in result.text


@pytest.mark.parametrize("name", ["voice.data", "voicenote"])
def test_unknown_or_missing_extension_is_detected(media_dir, name):
    source = _make(media_dir, "voice.mp3", ["-c:a", "libmp3lame"], video=False)
    target = media_dir / name
    shutil.copy(source, target)
    result = extract.from_file(target, name, Settings.from_env(), content_type="application/octet-stream")
    assert result.kind == "audio" and result.text == SPOKEN


def test_unknown_extension_video_is_detected(media_dir):
    source = _make(media_dir, "clip.mp4", H264 + ["-c:a", "aac"], video=True)
    target = media_dir / "clip.download"
    shutil.copy(source, target)
    result = extract.from_file(target, target.name, Settings.from_env())
    assert result.kind == "video" and _shows(result.text, SLIDE_A)


@pytest.mark.parametrize("name, codec", [("captions.mkv", "srt"), ("captions.mp4", "mov_text")])
def test_subtitle_tracks_are_read(media_dir, name, codec):
    srt = media_dir / "captions.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:03,000\n<i>Guaranteed cure in 7 days!</i>\n", encoding="utf-8")
    out = media_dir / name
    try:
        _ffmpeg("-loop", "1", "-framerate", "25", "-i", str(media_dir / "slide_a.png"), "-i",
                str(media_dir / "source.wav"), "-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s",
                "-t", str(SECONDS), *H264, "-c:a", "aac", "-c:s", codec, str(out))
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"cannot create {name}: {exc.stderr.decode(errors='ignore')[-160:]}")
    result = extract.from_file(out, name, Settings.from_env())
    assert "[Subtitles in the file]" in result.text and "Guaranteed cure in 7 days!" in result.text
    assert "subtitles in the file" in result.how


@pytest.mark.parametrize("name, fmt", [("ad.gif", "GIF"), ("ad.webp", "WEBP"), ("ad.apng", "PNG")])
def test_animated_images_read_every_scene(media_dir, name, fmt):
    a = Image.open(media_dir / "slide_a.png").convert("RGB")
    b = Image.open(media_dir / "slide_b.png").convert("RGB")
    path = media_dir / name
    a.save(path, format=fmt, save_all=True, append_images=[b], duration=800, loop=0)
    result = extract.from_file(path, name, Settings.from_env())
    assert result.kind == "image" and _shows(result.text, SLIDE_A) and _shows(result.text, SLIDE_B), result.text
    assert result.meta["frames_read"] == 2 and result.how == ["OCR of 2 animation frames"]


def test_subtitle_file_upload_is_cleaned(tmp_path):
    vtt = tmp_path / "reel.vtt"
    vtt.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nBest doctor in Pune\n\n00:02.000 --> 00:04.000\n"
                   "<b>50% off</b> this week\n", encoding="utf-8")
    result = extract.from_file(vtt, vtt.name, Settings.from_env())
    assert result.text == "Best doctor in Pune\n50% off this week"


def test_non_media_binary_is_rejected(tmp_path):
    junk = tmp_path / "data.bin"
    junk.write_bytes(bytes(range(256)) * 40)
    with pytest.raises(extract.ExtractionError, match="Unsupported file type"):
        extract.from_file(junk, junk.name, Settings.from_env())
