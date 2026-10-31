"""Guards for the Windows launchers and Windows-specific behaviour."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["setup.bat", "start.bat", "check-ai.bat", "check-audio.bat"])
def test_bat_files_are_ascii_with_crlf(name):
    data = (ROOT / name).read_bytes()
    data.decode("ascii")
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b""), "batch files need CRLF line endings"


@pytest.mark.parametrize("name", ["setup.ps1", "start.ps1"])
def test_powershell_scripts_are_ascii(name):
    # Windows PowerShell 5.1 reads BOM-less scripts as ANSI, so any non-ASCII character would break them
    (ROOT / "scripts" / name).read_bytes().decode("ascii")


def test_launchers_point_at_existing_scripts():
    for bat, script in [("setup.bat", "setup.ps1"), ("start.bat", "start.ps1")]:
        assert f"scripts\\{script}" in (ROOT / bat).read_text(encoding="ascii")
        assert (ROOT / "scripts" / script).exists()
    assert "-m app.ai_check" in (ROOT / "check-ai.bat").read_text(encoding="ascii")


@pytest.mark.parametrize("name", ["setup.bat", "start.bat", "check-ai.bat", "check-audio.bat"])
def test_bat_files_explain_running_from_inside_the_zip(name):
    text = (ROOT / name).read_text(encoding="ascii")
    assert "goto notextracted" in text and ":notextracted" in text and "Extract All" in text


def test_tesseract_cmd_setting_is_used(monkeypatch):
    import pytesseract

    from app.extract import ocr

    original = pytesseract.pytesseract.tesseract_cmd
    monkeypatch.setenv("TESSERACT_CMD", r"D:\Tools\Tesseract-OCR\tesseract.exe")
    try:
        ocr._configure_tesseract()
        assert pytesseract.pytesseract.tesseract_cmd == r"D:\Tools\Tesseract-OCR\tesseract.exe"
    finally:
        pytesseract.pytesseract.tesseract_cmd = original
