<#
.SYNOPSIS
    Launches the HolonBridge REST server, the MCP server, and the MCP
    server's ngrok tunnel together, from one script.

.DESCRIPTION
    Kills anything orphaned on the target ports first (the specific
    failure mode that prompted this script), then starts the REST
    bridge, the MCP server, and an ngrok tunnel for the MCP server, all
    as child processes with output redirected to timestamped log files
    rather than a single scrolling console. Watches all three and warns
    immediately if any of them dies. Ctrl+C tears everything down
    together, so there's no way to end up with one component running
    against a dead one.

    The REST bridge is NOT tunnelled -- per the project's own README,
    only the MCP layer is meant to be public; the REST bridge is reached
    over http://localhost:3031 by the MCP process and by nothing else.

    MCP_PUBLIC_URL is set here, in-process, from the same -McpNgrokUrl
    value that starts the tunnel, rather than left to whatever a
    separately-maintained .env says. Per the README, MCP_PUBLIC_URL
    drives both the OAuth host allowlist and the GitHub callback URL the
    MCP process expects -- if that value and the actual tunnel hostname
    ever disagree, the symptom is exactly an OAuth window that opens and
    never resolves. This removes the one place that mismatch could come
    from.

    CHANGED 2026-08-26: added the optional BootstrapAdmin* parameters,
    calling the new holonbridge-bootstrap-admin console script (see
    holonbridge/bootstrap.py) right after Fuseki comes up, before the
    REST bridge starts -- the bootstrap talks to Fuseki directly and
    doesn't need the bridge running at all. Solves the AnimusDep
    chicken-and-egg problem on a genuinely fresh dataset, where no route
    through the REST API can create the very first Person. Idempotent
    (checks by external id before writing, never touches an existing
    identity's role), so it is safe to leave set across every run --
    a no-op once the identity already exists. Leaving
    -BootstrapAdminGithubUser unset (the default) means nothing changes
    here at all.

.PARAMETER RepoPath
    Directory containing the holonbridge and holonbridge_mcp packages.

.PARAMETER VenvFolder
    Name of the venv folder under RepoPath. Defaults to ".venv" -- change
    this if yours is named differently.

.PARAMETER McpNgrokUrl
    Reserved ngrok hostname for the MCP server's tunnel. Defaults to the
    one seen in earlier session logs.

.PARAMETER RestNgrokUrl
    Reserved ngrok hostname for the REST bridge's tunnel, if you actually
    need one. Empty by default -- see DESCRIPTION.

.PARAMETER BootstrapAdminGithubUser
    GitHub login to bootstrap an identity for, directly against Fuseki,
    bypassing AnimusDep. Empty by default -- when unset, no bootstrap
    happens and nothing about this script's behaviour changes. Requires
    -BootstrapAdminSlug and -BootstrapAdminName to also be set.

.PARAMETER BootstrapAdminSlug
    Local identifier for the bootstrap identity, e.g. "kurt". Becomes the
    trailing segment of the Person IRI.

.PARAMETER BootstrapAdminName
    Display name (rdfs:label) for the bootstrap identity.

.PARAMETER BootstrapAdminRole
    Role slug to grant the bootstrap identity. Default "admin".

.PARAMETER BootstrapAdminDataset
    Dataset to bootstrap into. Defaults to the bank's own dataset when
    left empty.

.PARAMETER BootstrapAdminBank
    Named bank to bootstrap through (see ~/.holonbridge/config.json).
    Default "local".

.EXAMPLE
    .\start-holonbridge.ps1

.EXAMPLE
    .\start-holonbridge.ps1 -RestNgrokUrl "kurtcagle-rest-python.ngrok.io"

.EXAMPLE
    .\start-holonbridge.ps1 -BootstrapAdminGithubUser "kurtcagle" -BootstrapAdminSlug "kurt" -BootstrapAdminName "Kurt Cagle"
#>

param(
    [string]$RepoPath      = "C:\ProgramData\jena-bridge-python",
    [string]$VenvFolder    = ".venv",
    [string]$McpNgrokUrl   = "kurtcagle-mcp-python.ngrok.io",
    [string]$RestNgrokUrl  = "",
    [int]$RestPort         = 3031,
    [int]$McpPort          = 3034,

    # Fuseki -- primary (holongraph bank). Runs bare, no arguments -- its
    # dataset and location are already defined in its own config files.
    [string]$FusekiPath    = "C:\Apache\apache-jena-fuseki-6.1.0",
    [int]$FusekiPort1      = 3030,

    # Fuseki -- secondary (secondary bank)
    [int]$FusekiPort2      = 3040,
    [string]$FusekiBase2   = "C:\jena\fuseki-base2",
    [string]$FusekiLoc2    = "C:\jena\data2",
    [string]$FusekiDataset2 = "/ds",

    # Optional one-time-per-identity bootstrap admin (see DESCRIPTION).
    [string]$BootstrapAdminGithubUser = "",
    [string]$BootstrapAdminSlug       = "",
    [string]$BootstrapAdminName       = "",
    [string]$BootstrapAdminRole       = "admin",
    [string]$BootstrapAdminDataset    = "",
    [string]$BootstrapAdminBank       = "local"
)

$ErrorActionPreference = "Stop"

$LogDir = Join-Path $RepoPath "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Clear-Port {
    param([int]$Port)
    $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        Write-Host "  Killing orphaned process on port $Port (PID $($c.OwningProcess))" -ForegroundColor Yellow
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

function Start-Logged {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [string]$WorkingDirectory = $RepoPath
    )
    $outLog = Join-Path $LogDir "$Name-$timestamp.out.log"
    $errLog = Join-Path $LogDir "$Name-$timestamp.err.log"
    Write-Host "Starting $Name..."
    Write-Host "  -> logs: $outLog / $errLog"

    # Start-Process -ArgumentList has ValidateNotNullOrEmpty, so an empty
    # array (the holonbridge.exe case, which takes no arguments) throws if
    # passed directly. Splatting and omitting the key entirely when there's
    # nothing to pass avoids that.
    $params = @{
        FilePath                = $FilePath
        WorkingDirectory        = $WorkingDirectory
        NoNewWindow             = $true
        PassThru                = $true
        RedirectStandardOutput  = $outLog
        RedirectStandardError   = $errLog
    }
    if ($ArgumentList.Count -gt 0) {
        $params['ArgumentList'] = $ArgumentList
    }

    $proc = Start-Process @params
    return [PSCustomObject]@{ Name = $Name; Process = $proc; OutLog = $outLog; ErrLog = $errLog }
}

function Invoke-BootstrapAdmin {
    # Opt-in and idempotent -- see .DESCRIPTION above. Talks directly to
    # Fuseki (never through the REST bridge), which is why this runs
    # right after Fuseki comes up rather than after holonbridge itself.
    if (-not $BootstrapAdminGithubUser) {
        return
    }
    if (-not $BootstrapAdminSlug -or -not $BootstrapAdminName) {
        Write-Host "  -BootstrapAdminGithubUser is set but -BootstrapAdminSlug/-BootstrapAdminName are not -- skipping bootstrap admin" -ForegroundColor Yellow
        return
    }

    $bootstrapExe = Join-Path $RepoPath "$VenvFolder\Scripts\holonbridge-bootstrap-admin.exe"
    if (-not (Test-Path $bootstrapExe)) {
        Write-Host "  holonbridge-bootstrap-admin not found at $bootstrapExe -- skipping" -ForegroundColor Yellow
        Write-Host "  Rebuild the venv to pick it up: pip install -e `".[mcp,dev]`"" -ForegroundColor DarkGray
        return
    }

    $bootstrapArgs = @(
        "--slug", $BootstrapAdminSlug,
        "--name", $BootstrapAdminName,
        "--github-user", $BootstrapAdminGithubUser,
        "--role", $BootstrapAdminRole,
        "--bank", $BootstrapAdminBank
    )
    if ($BootstrapAdminDataset) {
        $bootstrapArgs += @("--dataset", $BootstrapAdminDataset)
    }

    Write-Host "Ensuring bootstrap identity for $BootstrapAdminGithubUser (idempotent)..."
    & $bootstrapExe @bootstrapArgs
}

Write-Host "Clearing ports $FusekiPort1, $FusekiPort2, $RestPort, and $McpPort of any orphaned processes..."
Clear-Port -Port $FusekiPort1
Clear-Port -Port $FusekiPort2
Clear-Port -Port $RestPort
Clear-Port -Port $McpPort
Start-Sleep -Seconds 1

$python = Join-Path $RepoPath "$VenvFolder\Scripts\python.exe"
$holonbridgeExe = Join-Path $RepoPath "$VenvFolder\Scripts\holonbridge.exe"
if (-not (Test-Path $python)) {
    throw "Couldn't find python.exe at $python -- check -VenvFolder matches your actual venv directory name."
}

# MCP_PUBLIC_URL drives both the OAuth host allowlist and the GitHub callback
# URL the MCP process expects. Setting it here, from the same parameter that
# starts the tunnel, means there is exactly one place this can drift instead
# of two (this value vs. whatever .env separately says) -- a mismatch between
# them is the most likely explanation for an OAuth window that opens but
# never resolves.
$env:MCP_PUBLIC_URL = "https://$McpNgrokUrl"

$handles = @()

$fusekiExe = Join-Path $FusekiPath "fuseki-server.bat"
if (-not (Test-Path $fusekiExe)) {
    throw "Couldn't find fuseki-server.bat at $fusekiExe -- check -FusekiPath."
}

# Bare call -- its own config files (not FUSEKI_BASE/--loc set here) already
# define the dataset and port, so nothing is passed on the command line.
$handles += Start-Logged -Name "fuseki1" -FilePath $fusekiExe -WorkingDirectory $FusekiPath -ArgumentList @()

$env:FUSEKI_BASE = $FusekiBase2
$handles += Start-Logged -Name "fuseki2" -FilePath $fusekiExe -WorkingDirectory $FusekiPath `
    -ArgumentList @("--port=$FusekiPort2", "--update", "--loc=$FusekiLoc2", $FusekiDataset2)
Remove-Item Env:\FUSEKI_BASE -ErrorAction SilentlyContinue

Start-Sleep -Seconds 3   # give Fuseki a moment to bind before the REST bridge starts calling it

Invoke-BootstrapAdmin

if (Test-Path $holonbridgeExe) {
    $handles += Start-Logged -Name "rest" -FilePath $holonbridgeExe -ArgumentList @()
} else {
    Write-Host "  (holonbridge.exe not found in venv Scripts -- falling back to python -m holonbridge.server)" -ForegroundColor DarkGray
    $handles += Start-Logged -Name "rest" -FilePath $python -ArgumentList @("-m", "holonbridge.server")
}
Start-Sleep -Seconds 3   # give the REST bridge a moment to bind before the MCP layer starts calling it

$handles += Start-Logged -Name "mcp" -FilePath $python -ArgumentList @("-m", "holonbridge_mcp", "--transport", "sse", "--port", "$McpPort")

$handles += Start-Logged -Name "ngrok-mcp" -FilePath "ngrok" -ArgumentList @("http", "--url=$McpNgrokUrl", "$McpPort")

if ($RestNgrokUrl) {
    $handles += Start-Logged -Name "ngrok-rest" -FilePath "ngrok" -ArgumentList @("http", "--url=$RestNgrokUrl", "$RestPort")
} else {
    Write-Host "No -RestNgrokUrl given -- skipping a tunnel for the REST bridge (see script header)." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "All processes started:" -ForegroundColor Green
foreach ($h in $handles) { Write-Host ("  {0,-12} PID {1}" -f $h.Name, $h.Process.Id) }
Write-Host ""
Write-Host "Watching for crashes. Press Ctrl+C to stop everything cleanly."
Write-Host ""

try {
    while ($true) {
        Start-Sleep -Seconds 2
        foreach ($h in $handles) {
            if ($h.Process.HasExited) {
                Write-Host "[$($h.Name)] exited unexpectedly (code $($h.Process.ExitCode)) -- check $($h.ErrLog)" -ForegroundColor Red
            }
        }
    }
}
finally {
    Write-Host ""
    Write-Host "Shutting down all processes..." -ForegroundColor Yellow
    foreach ($h in $handles) {
        if (-not $h.Process.HasExited) {
            Write-Host "  Stopping $($h.Name) (PID $($h.Process.Id))"
            Stop-Process -Id $h.Process.Id -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "Done."
}
