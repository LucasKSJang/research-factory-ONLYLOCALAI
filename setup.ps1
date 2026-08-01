# ============================================================
# Local Research Stack - Windows setup script
# Run from this folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# NOTE: This script is re-runnable. After installing Ollama or
# Docker Desktop it exits and asks you to re-run it (new PATH /
# daemon startup needed). Just run it again until you see DONE.
# ============================================================

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Test-Cmd($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

Write-Host ""
Write-Host "=== [1/6] Prerequisites check ===" -ForegroundColor Cyan

if (-not (Test-Cmd winget)) {
    Write-Host "winget not found. Install 'App Installer' from the Microsoft Store, then re-run this script." -ForegroundColor Red
    exit 1
}

# --- Ollama ---
if (-not (Test-Cmd ollama)) {
    Write-Host "Installing Ollama via winget..."
    winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
    Write-Host ""
    Write-Host ">>> Ollama installed. CLOSE this window, open a NEW PowerShell, and re-run setup.ps1" -ForegroundColor Yellow
    exit 0
}
Write-Host "Ollama: OK"

# --- Docker Desktop ---
if (-not (Test-Cmd docker)) {
    Write-Host "Installing Docker Desktop via winget (large download)..."
    winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
    Write-Host ""
    Write-Host ">>> Docker Desktop installed. Now:" -ForegroundColor Yellow
    Write-Host "    1) Reboot if Windows asks (WSL2 setup)"
    Write-Host "    2) Launch Docker Desktop and wait until it says 'Engine running'"
    Write-Host "    3) Re-run setup.ps1"
    exit 0
}
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is installed but the engine is not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait for 'Engine running', then re-run setup.ps1"
    exit 1
}
Write-Host "Docker: OK"

Write-Host ""
Write-Host "=== [2/6] Ollama context window ===" -ForegroundColor Cyan
# Default is 4096 which is too small for research summarization.
setx OLLAMA_CONTEXT_LENGTH 16384 | Out-Null
$env:OLLAMA_CONTEXT_LENGTH = "16384"
Write-Host "OLLAMA_CONTEXT_LENGTH=16384 set."
Write-Host "(Apply it: right-click the Ollama tray icon -> Quit, then start Ollama again)"

Write-Host ""
Write-Host "=== [3/6] Downloading models (approx 25GB total - this takes a while) ===" -ForegroundColor Cyan
ollama pull qwen3:30b          # main writer/synthesizer (MoE, ~19GB)
ollama pull qwen3:8b           # fast summarizer (~5GB)
ollama pull nomic-embed-text   # embeddings (~0.3GB)

Write-Host ""
Write-Host "=== [4/6] SearxNG secret key ===" -ForegroundColor Cyan
$settingsPath = Join-Path $PSScriptRoot "searxng\settings.yml"
# NOTE: do NOT use Get-Content/Set-Content here.
# Windows PowerShell 5.1 reads BOM-less files using the ANSI codepage. On a
# non-English Windows (e.g. Korean cp949) that mangles the non-ASCII comments in
# settings.yml into mojibake, adds a BOM, and can merge lines together - which
# comments out `secret_key:` and leaves SearxNG crash-looping on a YAML parse
# error. Read and write explicitly as UTF-8 without BOM instead.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$content = [System.IO.File]::ReadAllText($settingsPath, $utf8NoBom)
if ($content -match "REPLACE_ME_SECRET") {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    [System.IO.File]::WriteAllText($settingsPath, ($content -replace "REPLACE_ME_SECRET", $secret), $utf8NoBom)
    Write-Host "Secret key generated."
} else {
    Write-Host "Secret key already set - skipping."
}

Write-Host ""
Write-Host "=== [5/6] Folders ===" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path "files","files\reports" | Out-Null
if (-not (Test-Path "files\topics.txt")) {
    Copy-Item "topics.txt" "files\topics.txt"
    Write-Host "files\topics.txt created (edit this file to change research topics)."
} else {
    Write-Host "files\topics.txt already exists - keeping it."
}

Write-Host ""
Write-Host "=== [6/6] Starting containers ===" -ForegroundColor Cyan
docker compose up -d

Write-Host ""
Write-Host "=== DONE ===" -ForegroundColor Green
Write-Host ""
Write-Host "  SearxNG (search)      : http://localhost:8888"
Write-Host "  GPT Researcher (UI)   : http://localhost:8000"
Write-Host "  n8n (loop/scheduler)  : http://localhost:5678"
Write-Host ""
Write-Host "Quick sanity checks (see README.md section 4):"
Write-Host '  irm http://localhost:11434/api/tags'
Write-Host '  (irm "http://localhost:8888/search?q=bitcoin&format=json").results.Count'
Write-Host ""
Write-Host "Next: open README.md -> section 5 (first report) and 6 (n8n loop)."
