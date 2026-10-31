"""Audio failures must be recovered from where possible and otherwise explained, never hidden."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import check_audio, extract
from app.config import Settings
from app.engine.checker import NothingToCheck, run_check
from app.extract import Extraction, transcribe
from app.main import app
from tests.conftest import has_ffmpeg

client = TestClient(app)


class FakeWhisper:
    """Mimics faster_whisper.WhisperModel; can fail when the voice (VAD) filter is used."""

    def __init__(self, fail_with_vad=False, fail_always=False):
        self.fail_with_vad, self.fail_always, self.calls = fail_with_vad, fail_always, []

    def transcribe(self, audio, **kwargs):
        import numpy as np

        assert isinstance(audio, np.ndarray) and audio.dtype == np.float32, "pass samples, not a file path"
        self.calls.append(kwargs["vad_filter"])
        if self.fail_always or (self.fail_with_vad and kwargs["vad_filter"]):
            raise RuntimeError("Applying the VAD filter requires the onnxruntime package")
        segments = iter([SimpleNamespace(text=" Hello, this is Dr. Anil Mehra,"), SimpleNamespace(text=" MBBS.")])
        return segments, SimpleNamespace(language="en")


def _wav(tmp_path, seconds=1.0):
    import wave

    import numpy as np

    path = tmp_path / "a.wav"
    tone = (np.sin(np.linspace(0, 440 * 2 * np.pi * seconds, int(16000 * seconds))) * 12000).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1), wf.setsampwidth(2), wf.setframerate(16000), wf.writeframes(tone.tobytes())
    return path


def test_load_wav_gives_float_samples(tmp_path):
    import numpy as np

    audio = transcribe.load_wav(_wav(tmp_path, 2.0))
    assert audio.dtype == np.float32 and len(audio) == 32000 and 0.3 < np.abs(audio).max() <= 1.0


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")
def test_real_faster_whisper_never_uses_pyav_decoder(samples, monkeypatch, tmp_path):
    """Regression: faster-whisper 1.2.1 + PyAV 19 fail in decode_audio (metadata_errors). We pass samples."""
    import subprocess
    from types import SimpleNamespace

    fw = pytest.importorskip("faster_whisper.transcribe")

    def no_decoder(*a, **k):
        raise AssertionError("faster-whisper's PyAV decoder was used")

    monkeypatch.setattr(fw, "decode_audio", no_decoder)
    wav = tmp_path / "vn.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(samples / "voice_note_3_english_compliant.ogg"),
                    "-ac", "1", "-ar", "16000", str(wav)], check=True)
    audio = transcribe.load_wav(wav)
    try:  # a WhisperModel without weights: enough to run transcribe() up to the decoder model
        model = fw.WhisperModel.__new__(fw.WhisperModel)
        model.model = SimpleNamespace(is_multilingual=True)
        model.hf_tokenizer = SimpleNamespace(token_to_id=lambda t: 1, encode=lambda *a, **k: SimpleNamespace(ids=[1]))
        model.logger = __import__("logging").getLogger("fw")
        model.feature_extractor = fw.FeatureExtractor()
        model.feat_kwargs = {}
        model.frames_per_second, model.num_samples_per_token, model.input_stride = 100, 320, 2
        model.time_precision, model.max_length = 0.02, 448
        _, info = model.transcribe(audio, language="en", task="transcribe", vad_filter=True)
    except (AttributeError, TypeError) as exc:  # faster-whisper internals changed
        pytest.skip(f"cannot build a weightless WhisperModel for this faster-whisper version: {exc}")
    assert abs(info.duration - len(audio) / 16000) < 0.01 and info.duration_after_vad > 5


def test_voice_filter_failure_retries_without_it(monkeypatch, tmp_path):
    fake = FakeWhisper(fail_with_vad=True)
    monkeypatch.setattr(transcribe, "_local_model", lambda s: fake)
    result = transcribe.transcribe(_wav(tmp_path), None, Settings.from_env())
    assert fake.calls == [True, False]
    assert result.text == "Hello, this is Dr. Anil Mehra, MBBS."


def test_transcription_failure_is_explained(monkeypatch, tmp_path):
    monkeypatch.setattr(transcribe, "_local_model", lambda s: FakeWhisper(fail_always=True))
    with pytest.raises(transcribe.TranscriptionError, match="speech-to-text failed .RuntimeError"):
        transcribe.transcribe(_wav(tmp_path), None, Settings.from_env())


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")
def test_audio_without_words_says_why(samples, monkeypatch):
    monkeypatch.setattr(transcribe, "_local_model", lambda s: FakeWhisper(fail_always=True))
    with open(samples / "voice_note_3_english_compliant.ogg", "rb") as fh:
        r = client.post("/api/check", files={"file": ("note.ogg", fh, "audio/ogg")})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "No words could be taken from this audio" in detail and "RuntimeError" in detail


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")
def test_voice_note_end_to_end_with_working_speech(samples, monkeypatch):
    monkeypatch.setattr(transcribe, "_local_model", lambda s: FakeWhisper(fail_with_vad=True))
    with open(samples / "voice_note_3_english_compliant.ogg", "rb") as fh:
        r = client.post("/api/check", files={"file": ("WhatsApp Audio.ogg", fh, "audio/ogg")})
    assert r.status_code == 200
    assert r.json()["source"]["kind"] == "audio" and "Dr. Anil Mehra" in r.json()["text"]


def test_unexpected_errors_show_the_reason(samples, monkeypatch):
    from app.extract import media

    def boom(*a, **k):
        raise OSError("[WinError 32] The process cannot access the file because it is being used")

    monkeypatch.setattr(media, "probe", boom)
    monkeypatch.setattr(Settings, "ffmpeg_available", property(lambda self: True))
    with pytest.raises(extract.ExtractionError, match=r"Could not read this audio file \(OSError: \[WinError 32\]"):
        extract.from_file(samples / "voice_note_3_english_compliant.ogg", "note.ogg", Settings.from_env())


def test_unreadable_content_is_never_reported_compliant():
    with pytest.raises(NothingToCheck, match="Speech could not be transcribed"):
        run_check(Extraction(kind="video", text="", images=[b"jpeg"], warnings=["Speech could not be transcribed."]),
                  settings=Settings.from_env())


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")
def test_check_audio_command_success(monkeypatch, capsys):
    monkeypatch.setattr(transcribe, "_local_model", lambda s: FakeWhisper(fail_with_vad=True))
    assert check_audio.main([]) == 0
    out = capsys.readouterr().out
    assert "Heard in" in out and "Dr. Anil Mehra" in out and "Audio works" in out


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")
def test_check_audio_command_reports_model_download_problem(monkeypatch, capsys):
    def no_download(settings):
        raise ConnectionError("Failed to reach huggingface.co")

    monkeypatch.setattr(transcribe, "_local_model", no_download)
    assert check_audio.main([]) == 1
    out = capsys.readouterr().out
    assert "Whisper model could not be loaded (ConnectionError" in out and "TRANSCRIBE_BACKEND=openai" in out
