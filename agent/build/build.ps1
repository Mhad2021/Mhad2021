<#
.SYNOPSIS
    Build the Presence tracker agent into a single Windows executable.

.EXAMPLE
    .\build.ps1 -ServerUrl "https://presence.yourcompany.com"

.NOTES
    Run from the agent directory on a Windows machine with Python 3.11+.
#>
param(
    [Parameter(Mandatory = $true)][string]$ServerUrl,
    [string]$SignCertThumbprint = ""
)

$ErrorActionPreference = "Stop"
$AgentRoot = Split-Path -Parent $PSScriptRoot

Write-Host "Building Presence agent for $ServerUrl" -ForegroundColor Cyan
Push-Location $AgentRoot

try {
    if (-not (Test-Path ".venv")) {
        Write-Host "Creating build environment..."
        python -m venv .venv
    }
    & .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    & .\.venv\Scripts\python.exe -m pip install --quiet -r requirements-build.txt

    Write-Host "Running PyInstaller..."
    & .\.venv\Scripts\pyinstaller.exe build\presence-agent.spec --noconfirm --clean `
        --distpath build\dist --workpath build\work

    $exe = "build\dist\PresenceTracker.exe"
    if (-not (Test-Path $exe)) { throw "Build produced no executable" }

    # The installer reads this to write the machine-wide config.
    @{ server_url = $ServerUrl; verify_tls = $true } |
        ConvertTo-Json | Set-Content "build\dist\config.json" -Encoding UTF8

    if ($SignCertThumbprint) {
        Write-Host "Signing the executable..."
        & signtool.exe sign /fd SHA256 /sha1 $SignCertThumbprint `
            /tr http://timestamp.digicert.com /td SHA256 $exe
    } else {
        Write-Warning "Not signed. Windows SmartScreen will warn on first run."
        Write-Warning "Re-run with -SignCertThumbprint to sign it."
    }

    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "Built $exe ($size MB)" -ForegroundColor Green
    Write-Host "Next: compile the installer with Inno Setup (build\installer.iss)"
}
finally {
    Pop-Location
}
