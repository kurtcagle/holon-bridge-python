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

.EXAMPLE
    .\start-holonbridge.ps1

.EXAMPLE
    .\start-holonbridge.ps1 -RestNgrokUrl "kurtcagle-rest-python.ngrok.io"
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
    [string]$FusekiDataset2 = "/ds"
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
