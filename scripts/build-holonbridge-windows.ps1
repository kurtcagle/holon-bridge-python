<#
.SYNOPSIS
    One-time (re-runnable) build of kurtcagle/holon-bridge-python on
    Windows 11 / PowerShell.

.DESCRIPTION
    The PowerShell counterpart to scripts/build-holonbridge-ubuntu.sh.
    Same shape, same non-goals:

    Does NOT install or configure Fuseki -- FusekiPath in
    start-holonbridge.ps1 must already point at a real Jena install.
    Does NOT start anything -- once this finishes, use
    scripts\start-holonbridge.ps1 to run the stack.
    Does NOT touch ngrok config -- run 'ngrok config add-authtoken
    <token>' yourself.

    What it does:
      1. Verifies Python >=3.10 and git are available (installs via
         winget if entirely missing; does NOT silently upgrade an
         existing-but-too-old Python)
      2. Clones or updates the repo at -InstallDir (git, matching the
         project's preference for git over the GitHub MCP on full
         checkouts)
      3. Creates/reuses a venv at -InstallDir\-VenvFolder
      4. pip installs the package in editable mode, pinned via
         constraints.txt -- calls venv Scripts\pip.exe directly rather
         than dot-sourcing Activate.ps1, so this runs the same way
         regardless of the caller's execution policy
      5. Seeds .env from .env.example if missing, auto-generating
         BEARER_TOKEN the way .env.example itself documents for
         Windows (RNGCryptoServiceProvider -> Base64, not openssl)
      6. Verifies holonbridge.exe / holonbridge-mcp.exe exist in the venv

    -InstallDir defaults to the same path start-holonbridge.ps1 already
    assumes (C:\ProgramData\jena-bridge-python) so that script's own
    defaults work unmodified afterward.

.PARAMETER InstallDir
    Where to clone/update the repo. Default matches start-holonbridge.ps1.

.PARAMETER VenvFolder
    Venv folder name under InstallDir. Default matches start-holonbridge.ps1.

.PARAMETER Branch
    Git branch to build. Default "main".

.PARAMETER Extras
    pip extras to install, comma-separated inside the brackets pip expects
    (e.g. "mcp,shacl,dev"). shacl is included by default -- SHACL_REQUIRED
    is live on real deployments, unlike the README's minimal mcp,dev example.

.PARAMETER PythonExe
    Python launcher to use for creating the venv. Default "python".

.PARAMETER ResetVenv
    Remove and recreate the venv even if one already exists.

.EXAMPLE
    .\build-holonbridge-windows.ps1

.EXAMPLE
    .\build-holonbridge-windows.ps1 -InstallDir "D:\holon-bridge-python" -Branch main
#>

param(
    [string]$InstallDir = "C:\ProgramData\jena-bridge-python",
    [string]$VenvFolder = ".venv",
    [string]$Branch     = "main",
    [string]$Extras     = "mcp,shacl,dev",
    [string]$PythonExe  = "python",
    [switch]$ResetVenv
)

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/kurtcagle/holon-bridge-python.git"
$MinPyMinor = 10   # pyproject.toml requires-python = ">=3.10"

function Log  { param([string]$Msg) Write-Host "[build-holonbridge] $Msg" }
function Err  { param([string]$Msg) Write-Host "[build-holonbridge] ERROR: $Msg" -ForegroundColor Red }

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------

if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Err "This script targets Windows. Detected platform: $($PSVersionTable.Platform)"
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Prerequisites: git, Python >=3.10
# ---------------------------------------------------------------------------

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if (-not (Test-Command "git")) {
    if (Test-Command "winget") {
        Log "git not found -- installing via winget..."
        winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
    } else {
        Err "git not found and winget is unavailable. Install Git for Windows manually: https://git-scm.com/download/win"
        exit 1
    }
}

if (-not (Test-Command $PythonExe)) {
    if (Test-Command "winget") {
        Log "$PythonExe not found -- installing via winget..."
        winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
        Err "Python was just installed -- open a new PowerShell window (PATH needs to refresh) and re-run this script."
        exit 1
    } else {
        Err "$PythonExe not found and winget is unavailable. Install Python >=3.$MinPyMinor manually: https://www.python.org/downloads/"
        exit 1
    }
}

$verOutput = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
$verParts = $verOutput.Trim().Split('.')
$pyMajor = [int]$verParts[0]
$pyMinor = [int]$verParts[1]
if ($pyMajor -ne 3 -or $pyMinor -lt $MinPyMinor) {
    Err "$PythonExe is $pyMajor.$pyMinor -- holonbridge requires >=3.$MinPyMinor"
    Err "This is an existing install that's too old -- not auto-upgrading it deliberately."
    Err "Install a newer Python (e.g. 'winget install Python.Python.3.12') and re-run with"
    Err "  -PythonExe <path to the newer python.exe>"
    exit 1
}
Log "Using Python $pyMajor.$pyMinor"

# ---------------------------------------------------------------------------
# 2. Clone or update the repo
# ---------------------------------------------------------------------------

if (-not (Test-Path $InstallDir)) {
    Log "Creating $InstallDir and cloning..."
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $InstallDir -Parent) -ErrorAction Stop | Out-Null
        git clone --branch $Branch $RepoUrl $InstallDir
    } catch [System.UnauthorizedAccessException] {
        Err "Access denied creating $InstallDir -- re-run this script from an elevated (Administrator) PowerShell."
        exit 1
    }
} elseif (Test-Path (Join-Path $InstallDir ".git")) {
    Log "Updating existing checkout at $InstallDir..."
    git -C $InstallDir fetch origin $Branch
    git -C $InstallDir checkout $Branch
    git -C $InstallDir pull --ff-only origin $Branch
} else {
    Err "$InstallDir exists but isn't a git checkout -- refusing to overwrite it."
    Err "Move it aside or pass a fresh -InstallDir."
    exit 1
}

Set-Location $InstallDir

# ---------------------------------------------------------------------------
# 3. Virtualenv
# ---------------------------------------------------------------------------

$VenvDir = Join-Path $InstallDir $VenvFolder
if ($ResetVenv -and (Test-Path $VenvDir)) {
    Log "-ResetVenv passed -- removing existing venv..."
    Remove-Item -Recurse -Force $VenvDir
}

if (-not (Test-Path $VenvDir)) {
    Log "Creating venv at $VenvDir..."
    & $PythonExe -m venv $VenvDir
} else {
    Log "Reusing existing venv at $VenvDir"
}

$venvPython = Join-Path $VenvDir "Scripts\python.exe"
$venvPip    = Join-Path $VenvDir "Scripts\pip.exe"
if (-not (Test-Path $venvPython)) {
    Err "venv creation didn't produce $venvPython -- something went wrong above."
    exit 1
}

& $venvPython -m pip install --upgrade pip --quiet

# ---------------------------------------------------------------------------
# 4. Install the package (constrained, matching the repo's own README recipe)
# ---------------------------------------------------------------------------

Log "Installing holonbridge[$Extras] with constraints.txt..."
$constraintsPath = Join-Path $InstallDir "constraints.txt"
if (Test-Path $constraintsPath) {
    & $venvPip install -c $constraintsPath -e ".[$Extras]"
} else {
    Log "No constraints.txt found -- installing unconstrained"
    & $venvPip install -e ".[$Extras]"
}

# ---------------------------------------------------------------------------
# 5. .env
# ---------------------------------------------------------------------------

$EnvFile = Join-Path $InstallDir ".env"
$EnvExample = Join-Path $InstallDir ".env.example"
if (-not (Test-Path $EnvFile)) {
    if (Test-Path $EnvExample) {
        Log "Seeding .env from .env.example..."
        Copy-Item $EnvExample $EnvFile

        # Same method .env.example itself documents for Windows PowerShell --
        # works on both 5.1 and 7+ (the static ::GetBytes(32) overload used
        # on 7+ needs .NET 6+ and isn't available on 5.1's CLR).
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $token = [Convert]::ToBase64String($bytes)

        (Get-Content $EnvFile) -replace '^BEARER_TOKEN=.*', "BEARER_TOKEN=$token" |
            Set-Content $EnvFile
        Log "Generated a fresh BEARER_TOKEN and wrote it into .env"
    } else {
        Err ".env.example not found in $InstallDir -- skipping .env setup"
    }
} else {
    Log ".env already exists -- leaving it untouched"
}

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------

Log "Verifying console scripts..."
$missing = $false
foreach ($bin in @("holonbridge.exe", "holonbridge-mcp.exe")) {
    $binPath = Join-Path $VenvDir "Scripts\$bin"
    if (Test-Path $binPath) {
        Log "  OK: $binPath"
    } else {
        Err "  MISSING: $binPath -- install did not complete cleanly"
        $missing = $true
    }
}
if ($missing) { exit 1 }

Write-Host ""
Write-Host "[build-holonbridge] Build complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Install dir : $InstallDir"
Write-Host "  Venv        : $VenvDir"
Write-Host "  Env file    : $EnvFile"
Write-Host ""
Write-Host "Still manual, by design:"
Write-Host "  - Fuseki itself (FusekiPath in start-holonbridge.ps1 must point at a"
Write-Host "    real Jena install -- this script never touches Fuseki)"
Write-Host "  - Any secrets .env didn't get auto-filled: ANTHROPIC_API_KEY,"
Write-Host "    GITHUB_OAUTH_CLIENT_ID/SECRET, MCP_PUBLIC_URL, MCP_JWT_SECRET,"
Write-Host "    MCP_INBOUND_TOKEN, MCP_ALLOWED_GITHUB_LOGINS -- see README.md >"
Write-Host "    'GitHub OAuth, as a second credential kind' if doing remote MCP"
Write-Host "  - ngrok: run 'ngrok config add-authtoken <token>' yourself (this"
Write-Host "    script deliberately doesn't touch ngrok config)"
Write-Host ""
Write-Host "Next:"
Write-Host "  cd $InstallDir"
Write-Host "  .\scripts\start-holonbridge.ps1"
