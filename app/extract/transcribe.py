"""Speech-to-text for audio and video.

Backends (TRANSCRIBE_BACKEND):
- local  : faster-whisper on this machine (default). The model downloads on first use.
           WHISPER_MODEL=small is quick on CPU; large-v3-turbo is much better for Hindi/Hinglish.
- openai : any OpenAI-compatible /audio/transcriptions API (OpenAI, Groq, ...).
- auto   : openai when OPENAI_API_KEY is set, otherwise local (used by the free website on Render).
- none   : skip speech; only on-screen text is read from videos.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings

log = logging.getLogger(__name__)


class TranscriptionError(RuntimeError):
    pass


@dataclass
class Transcript:
    text: str
    language: str | None
    backend: str


_model_lock = threading.Lock()
_models: dict[tuple[str, str, str], object] = {}


def _local_model(settings: Settings):
    key = (settings.whisper_model, settings.whisper_device, settings.whisper_compute_type,
           settings.whisper_cpu_threads)
    with _model_lock:
        if key not in _models:
            from faster_whisper import WhisperModel

            log.info("Loading faster-whisper model %s (%s/%s, threads %s)...", *key)
            _models[key] = WhisperModel(
                settings.whisper_model, device=settings.whisper_device, compute_type=settings.whisper_compute_type,
                cpu_threads=settings.whisper_cpu_threads,
            )
        return _models[key]


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def load_wav(path: Path):
    """Read the 16 kHz mono 16-bit WAV that ffmpeg produced into float32 samples in [-1, 1].

    Giving faster-whisper the samples (instead of a file path) skips its own PyAV-based decoder,
    which breaks with newer PyAV releases (TypeError: unexpected keyword argument 'metadata_errors').
    """
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2 or wf.getframerate() != 16000:
            raise TranscriptionError("Internal audio conversion produced an unexpected WAV format.")
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio


def _transcribe_local(wav_path: Path, language: str | None, settings: Settings) -> Transcript:
    try:
        model = _local_model(settings)
    except ImportError as exc:
        raise TranscriptionError(f"faster-whisper could not be loaded ({_describe(exc)})") from exc
    except Exception as exc:
        raise TranscriptionError(
            f"Could not load the Whisper model '{settings.whisper_model}' ({_describe(exc)}). "
            "The first run downloads it, so check the internet connection or proxy."
        ) from exc

    audio = load_wav(wav_path)

    def run(vad: bool):
        segments, info = model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=settings.whisper_beam_size,
            vad_filter=vad,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip(), info

    with _model_lock:  # one transcription at a time per process keeps CPU memory predictable
        try:
            text, info = run(vad=True)
        except Exception as first:
            # The voice-activity filter needs onnxruntime, which on Windows often fails to load when the
            # Microsoft Visual C++ runtime is missing. Speech-to-text itself works without it.
            log.warning("Transcription with the voice filter failed (%s); retrying without it", _describe(first))
            try:
                text, info = run(vad=False)
            except Exception as second:
                log.exception("Transcription failed")
                raise TranscriptionError(f"speech-to-text failed ({_describe(second)})") from second
    return Transcript(text=text, language=getattr(info, "language", language), backend="local")


def _transcribe_openai(wav_path: Path, language: str | None, settings: Settings) -> Transcript:
    import httpx

    if not settings.openai_api_key:
        raise TranscriptionError("TRANSCRIBE_BACKEND=openai needs OPENAI_API_KEY")
    data = {"model": settings.openai_transcribe_model, "response_format": "json"}
    if language:
        data["language"] = language
    with wav_path.open("rb") as fh:
        response = httpx.post(
            f"{settings.openai_base_url}/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            data=data,
            files={"file": (wav_path.name, fh, "audio/wav")},
            timeout=300,
        )
    if response.status_code >= 400:
        raise TranscriptionError(f"Transcription API error {response.status_code}: {response.text[:300]}")
    return Transcript(text=response.json().get("text", "").strip(), language=language, backend="openai")


def transcribe(wav_path: Path, language: str | None, settings: Settings) -> Transcript:
    """Transcribe a 16 kHz mono WAV file. ``language`` is an ISO code ("en", "hi") or None for auto."""
    backend = settings.speech_backend
    if backend == "none":
        raise TranscriptionError("Speech-to-text is switched off (TRANSCRIBE_BACKEND=none)")
    if backend == "openai":
        return _transcribe_openai(wav_path, language, settings)
    if backend == "local":
        return _transcribe_local(wav_path, language, settings)
    raise TranscriptionError(f"Unknown TRANSCRIBE_BACKEND '{backend}'")
