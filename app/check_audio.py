"""Find out why audio notes or videos fail on this computer.

    python -m app.check_audio                         # tests with samples/voice_note_3_english_compliant.ogg
    python -m app.check_audio --file "C:\\path\\note.ogg"  # tests with your own file

Each step prints OK / WARN / FAIL with what to do. Exit code 0 = audio works.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE = ROOT / "samples" / "voice_note_3_english_compliant.ogg"
VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"


def _say(tag: str, text: str) -> None:
    print(f"   [{tag:<4}] {text}")


def _first_line(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (out.stdout or out.stderr).strip().splitlines()[0][:80]
    except Exception as exc:  # noqa: BLE001
        return f"could not run ({type(exc).__name__})"


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    logging.getLogger("app").setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Check speech-to-text for audio notes and videos.")
    parser.add_argument("--file", type=Path, default=DEFAULT_SAMPLE, help="audio or video file to test with")
    args = parser.parse_args(argv)

    from app import extract
    from app.config import Settings
    from app.engine.checker import NothingToCheck, run_check
    from app.extract import transcribe

    settings = Settings.from_env()
    ok = True
    print("AdRx - audio and video check\n")

    print("1) ffmpeg (reads audio and video files)")
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if path:
            _say("OK", f"{tool}: {path} ({_first_line([path, '-version'])})")
        else:
            _say("FAIL", f"{tool} not found. Install it (Windows: winget install Gyan.FFmpeg), "
                         "then close and reopen the app window.")
            ok = False
    if not ok:
        return 1

    backend = settings.speech_backend
    print(f"\n2) Speech-to-text: {backend}")
    if backend == "none":
        _say("WARN", "TRANSCRIBE_BACKEND=none in .env, so speech is not transcribed. Set it to local.")
        return 1
    if backend == "openai":
        if not settings.openai_api_key:
            _say("FAIL", "TRANSCRIBE_BACKEND=openai needs OPENAI_API_KEY in .env.")
            return 1
        _say("OK", f"Using {settings.openai_base_url} with model {settings.openai_transcribe_model}")
    elif backend == "local":
        try:
            import faster_whisper

            _say("OK", f"faster-whisper {getattr(faster_whisper, '__version__', '')} loaded")
        except Exception as exc:  # noqa: BLE001
            _say("FAIL", f"faster-whisper could not load ({type(exc).__name__}: {exc}).")
            if os.name == "nt":
                _say("INFO", f"Install the Microsoft Visual C++ runtime ({VC_REDIST}), restart, run setup.bat.")
            return 1
        try:
            import onnxruntime

            _say("OK", f"onnxruntime {onnxruntime.__version__} loaded (voice filter)")
        except Exception as exc:  # noqa: BLE001
            _say("WARN", f"onnxruntime could not load ({type(exc).__name__}: {str(exc)[:120]}).")
            _say("INFO", "Speech-to-text still works: the app skips the voice filter."
                 + (f" To fix it, install the Microsoft Visual C++ runtime: {VC_REDIST}" if os.name == "nt" else ""))
        print(f"   ...loading Whisper model '{settings.whisper_model}' (the first time it downloads; can take minutes)")
        started = time.monotonic()
        try:
            transcribe._local_model(settings)
            _say("OK", f"Whisper model '{settings.whisper_model}' ready ({time.monotonic() - started:.0f} s)")
        except Exception as exc:  # noqa: BLE001
            _say("FAIL", f"Whisper model could not be loaded ({type(exc).__name__}: {str(exc)[:200]})")
            _say("INFO", "Usually the download is blocked (no internet, office proxy or VPN). Try another "
                         "network, or set TRANSCRIBE_BACKEND=openai in .env to use an online service.")
            return 1
    else:
        _say("FAIL", f"Unknown TRANSCRIBE_BACKEND '{backend}' in .env (use local, openai or none).")
        return 1

    print(f"\n3) Transcribe {args.file.name}")
    if not args.file.exists():
        _say("FAIL", f"File not found: {args.file}")
        return 1
    started = time.monotonic()
    try:
        extraction = extract.from_file(args.file, args.file.name, settings)
    except extract.ExtractionError as exc:
        _say("FAIL", str(exc))
        return 1
    heard = extraction.text.replace("\n", " ")
    _say("OK", f"Heard in {time.monotonic() - started:.0f} s: \"{heard[:160]}{'...' if len(heard) > 160 else ''}\"")
    for warning in extraction.warnings:
        _say("WARN", warning)

    print("\n4) Check it against the guidelines")
    try:
        result = run_check(extraction, settings=settings, publisher="rmp", channel="whatsapp")
    except NothingToCheck as exc:
        _say("FAIL", str(exc))
        return 1
    _say("OK", f"{result['verdict']}: {result['headline']}")
    print("\nAudio works. If a voice note still fails in the browser, send the file logs\\adrx.log.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
