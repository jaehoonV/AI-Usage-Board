# Build AI Usage Board into dist\AIUsageBoard\AIUsageBoard.exe
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# PyInstaller --clean deletes dist\AIUsageBoard before writing it again. With
# the exe running, the locked files survive and everything else - resources
# included - is deleted, leaving the running copy and the next launch broken.
$exe = Join-Path $PSScriptRoot "dist\AIUsageBoard\AIUsageBoard.exe"
$running = Get-Process AIUsageBoard -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ($_.Path -ieq $exe) }
if ($running) {
    Write-Error "AIUsageBoard.exe is running from dist. Quit it first (settings menu > 종료, or tray icon > 종료)."
    exit 1
}

# The app itself is stdlib-only; prefer a mature interpreter for PyInstaller.
# Asked of the launcher once rather than by running each candidate: a missing
# version makes `py -3.12` write to stderr, which PowerShell turns into an
# error record, which $ErrorActionPreference = "Stop" turns into the end of
# the script - and the fallback below would never run.
$preferred = @("3.12", "3.11", "3.13")
$installed = ""
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try { $installed = (& py -0p 2>$null | Out-String) } catch { $installed = "" }
$ErrorActionPreference = $previous

$chosen = $null
foreach ($version in $preferred) {
    if ($installed -match "(-V:|-)$([regex]::Escape($version))(\s|-|$)") { $chosen = $version; break }
}
if (-not $chosen) {
    Write-Warning "3.11-3.13 중 설치된 버전이 없어 기본 인터프리터를 사용합니다."
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv ..."
    if ($chosen) { & py "-$chosen" -m venv .venv } else { & py -m venv .venv }
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -c "import sys; print('venv python', sys.version.split()[0])"
& $python -m pip install --upgrade pip
& $python -m pip install pyinstaller

Write-Host "Regenerating icon ..."
& $python tools\make_icon.py

Write-Host "Building ..."
& $python -m PyInstaller --noconfirm --clean ai_usage_board.spec
# A native command's failure does not trip $ErrorActionPreference; without this
# check the script would print "Done" over a failed build.
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed (exit $LASTEXITCODE). dist\AIUsageBoard may be incomplete."
    exit $LASTEXITCODE
}

# The license travels with every copy of the build.
Copy-Item -LiteralPath LICENSE -Destination dist\AIUsageBoard\LICENSE -Force

Write-Host ""
Write-Host "Done -> dist\AIUsageBoard\AIUsageBoard.exe"
