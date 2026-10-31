# AdRx one-time setup: checks the tools, creates .venv, installs packages, creates .env, runs the self-test.
# Easiest: double-click setup.bat in the project folder.
# Or from a terminal: powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$onWindows = ($env:OS -eq 'Windows_NT')
$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

function Say([string]$tag, [string]$text) {
    $colors = @{ 'OK' = 'Green'; 'WARN' = 'Yellow'; 'FAIL' = 'Red'; 'INFO' = 'Gray' }
    Write-Host ('  [{0,-4}] {1}' -f $tag, $text) -ForegroundColor $colors[$tag]
}

function Step([string]$text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Cyan
}

function Ask-YesNo([string]$question) {
    $answer = Read-Host "  $question [Y/n]"
    return ($answer -eq '' -or $answer -match '^[Yy]')
}

function Update-SessionPath {
    if ($onWindows) {
        $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $user = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = "$machine;$user"
    }
}

function Find-Python {
    $candidates = @(
        @{ Exe = 'py'; Args = @('-3.13') },
        @{ Exe = 'py'; Args = @('-3.12') },
        @{ Exe = 'py'; Args = @('-3.11') },
        @{ Exe = 'python'; Args = @() },
        @{ Exe = 'python3'; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $probe = $c.Args + @('-c', 'import sys; print(sys.version_info[0] * 100 + sys.version_info[1])')
            $version = & $c.Exe $probe 2>$null
            if ($LASTEXITCODE -eq 0 -and [int]"$version" -ge 311) { return $c }
        } catch { }
    }
    return $null
}

function Find-Tesseract {
    $cmd = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Path }
    $paths = @(
        "$env:ProgramFiles\Tesseract-OCR\tesseract.exe",
        "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe",
        "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe"
    )
    foreach ($p in $paths) {
        if ($env:ProgramFiles -and (Test-Path $p)) { return $p }
    }
    return $null
}

function Install-WithWinget([string]$id, [string]$question, [switch]$Interactive) {
    if (-not ($onWindows -and $hasWinget)) { return }
    if (-not (Ask-YesNo $question)) { return }
    $wingetArgs = @('install', '--id', $id, '-e', '--source', 'winget',
                    '--accept-package-agreements', '--accept-source-agreements')
    if ($Interactive) { $wingetArgs += '--interactive' }
    & winget $wingetArgs
    Update-SessionPath
}

Write-Host 'AdRx setup' -ForegroundColor Cyan
Write-Host "Project folder: $root"

# 1) Python ------------------------------------------------------------------------
Step '1) Python 3.11 or newer'
$py = Find-Python
if (-not $py) {
    Say 'WARN' 'Python 3.11+ was not found.'
    Install-WithWinget 'Python.Python.3.12' 'Install Python 3.12 now with winget?'
    $py = Find-Python
}
if (-not $py) {
    Say 'FAIL' 'Python is needed. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then run setup.bat again.'
    exit 1
}
$pyVersion = & $py.Exe ($py.Args + @('--version'))
Say 'OK' "$pyVersion"

# 2) ffmpeg ------------------------------------------------------------------------
Step '2) ffmpeg (reads audio notes and videos)'
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Say 'WARN' 'ffmpeg was not found.'
    Install-WithWinget 'Gyan.FFmpeg' 'Install ffmpeg now with winget?'
}
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Say 'OK' 'ffmpeg found'
} else {
    Say 'WARN' 'Audio and video checks need ffmpeg (winget install Gyan.FFmpeg). Text, PDF, Word and images still work.'
}

# 3) Tesseract ---------------------------------------------------------------------
Step '3) Tesseract OCR (reads text in posters, scanned PDFs and video frames)'
$tesseract = Find-Tesseract
if (-not $tesseract) {
    Say 'WARN' 'Tesseract was not found.'
    Say 'INFO' 'In the installer, open "Additional language data" and tick Hindi.'
    Install-WithWinget 'UB-Mannheim.TesseractOCR' 'Install Tesseract now with winget?' -Interactive
    $tesseract = Find-Tesseract
}
if ($tesseract) {
    $tessVersion = (& $tesseract --version 2>&1 | ForEach-Object { "$_" } | Select-Object -First 1)
    Say 'OK' "Tesseract found: $tesseract ($tessVersion)"
    if ("$tessVersion" -match '(\d+)\.\d+' -and [int]$Matches[1] -lt 5) {
        Say 'WARN' 'This Tesseract is old and reads text in videos less well. Update it: winget upgrade UB-Mannheim.TesseractOCR'
    }
    $languages = (& $tesseract --list-langs 2>&1 | Out-String)
    if ($languages -match '(?m)^\s*hin\s*$') {
        Say 'OK' 'Hindi OCR data is installed'
    } else {
        Say 'WARN' 'Hindi OCR data is missing, so Hindi posters cannot be read. Re-run the Tesseract installer and tick Hindi.'
    }
} else {
    Say 'WARN' 'Text inside images and scanned PDFs will not be read until Tesseract is installed (winget install UB-Mannheim.TesseractOCR).'
}

# 4) Packages ----------------------------------------------------------------------
Step '4) Python packages (the first time takes a few minutes)'
if ($onWindows) { $venvPython = Join-Path $root '.venv\Scripts\python.exe' } else { $venvPython = Join-Path $root '.venv/bin/python' }
if (-not (Test-Path $venvPython)) {
    & $py.Exe ($py.Args + @('-m', 'venv', '.venv'))
    if (-not (Test-Path $venvPython)) {
        Say 'FAIL' 'Could not create the virtual environment (.venv).'
        exit 1
    }
    Say 'OK' 'Created .venv'
}
& $venvPython -m pip install --upgrade pip --quiet --disable-pip-version-check
& $venvPython -m pip install -r requirements-dev.txt --disable-pip-version-check
if ($LASTEXITCODE -ne 0) {
    Say 'FAIL' 'Installing the packages failed. Check your internet connection and the messages above, then run setup.bat again.'
    exit 1
}
Say 'OK' 'Packages installed'

# 5) Settings ----------------------------------------------------------------------
Step '5) Settings file (.env)'
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Say 'OK' 'Created .env from .env.example'
} else {
    Say 'OK' '.env already exists (left unchanged)'
}
$envText = Get-Content '.env' -Raw
if ($envText -match '(?m)^ANTHROPIC_API_KEY=\s*sk-ant-') {
    Say 'OK' 'Anthropic API key found: AI review will be on'
} else {
    Say 'INFO' 'No Anthropic API key yet: the app runs in quick-check mode (phrase rules).'
    Say 'INFO' 'For AI review, put your key after ANTHROPIC_API_KEY= in .env and run check-ai.bat.'
}

# 6) Self-test ---------------------------------------------------------------------
Step '6) Self-test'
& $venvPython -m pytest -q -p no:cacheprovider
if ($LASTEXITCODE -eq 0) {
    Say 'OK' 'All tests passed'
} else {
    Say 'WARN' 'Some tests failed (see above). The app may still work; share this output if you need help.'
}

Write-Host ''
Write-Host 'Setup finished. Next:' -ForegroundColor Cyan
Write-Host '  - Double-click start.bat: the app opens in your browser at http://localhost:8000'
Write-Host '  - Optional: add your Anthropic key to .env, then double-click check-ai.bat'
Write-Host '  - If a voice note or video does not work, double-click check-audio.bat'
exit 0
