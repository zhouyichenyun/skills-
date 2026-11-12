param(
    [string]$Destination = (Join-Path $env:USERPROFILE ".codex\skills"),
    [switch]$Update,
    [switch]$InstallLocalASR,
    [ValidateSet("none", "tiny", "small")]
    [string]$ASRModel = "none"
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.10+ is required. Install Python with its Windows launcher and PATH enabled."
}
python -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'"
if ($LASTEXITCODE -ne 0) { throw "Python version check failed." }
$InstallArgs = @((Join-Path $RepoRoot "scripts\install_local.py"), "--dest", $Destination)
if ($Update) { $InstallArgs += "--update" }
& python @InstallArgs
if ($LASTEXITCODE -ne 0) { throw "Skill installation failed." }
foreach ($CommandName in @("ffmpeg", "ffprobe", "node")) {
    if (Get-Command $CommandName -ErrorAction SilentlyContinue) {
        Write-Host "OK: $CommandName"
    } else {
        Write-Warning "$CommandName is missing from PATH. Media processing remains unavailable until it is installed."
    }
}
if ($InstallLocalASR -or $ASRModel -ne "none") {
    $VenvPath = Join-Path $RepoRoot ".venv"
    python -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw "ASR virtual environment creation failed." }
    $VenvPython = Join-Path $VenvPath "Scripts\python.exe"
    & $VenvPython -m pip install -r (Join-Path $RepoRoot "skills\talking-head-auto-editor\requirements-optional.txt")
    if ($LASTEXITCODE -ne 0) { throw "Local ASR dependency installation failed." }
    Write-Host "ASR dependencies installed. Existing local models may be used directly; -ASRModel explicitly prepares one below. Audio is not uploaded."
}
if ($ASRModel -ne "none") {
    $ModelDirectory = Join-Path $RepoRoot ("models\faster-whisper-" + $ASRModel)
    & python (Join-Path $RepoRoot "scripts\prepare_asr_model.py") --model $ASRModel --dest $ModelDirectory
    if ($LASTEXITCODE -ne 0) { throw "ASR model preparation failed." }
    Write-Host "Local model ready: $ModelDirectory"
}
Write-Host "Skills installed for this Windows account. Restart/reload your agent to refresh skill discovery."
Write-Host "Resolve and Jianying desktop connection still requires the checks in windows/README.md."
