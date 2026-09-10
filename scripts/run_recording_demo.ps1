param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765,
    [switch]$Offline,
    [ValidateRange(10, 600)]
    [int]$DockerWaitSeconds = 120
)

$ErrorActionPreference = 'Stop'

function Get-DemoExistingService {
    param([int]$DemoPort, [bool]$OfflineRequested)

    $demoConnection = New-Object System.Net.Sockets.TcpClient
    try {
        $demoConnectTask = $demoConnection.ConnectAsync('127.0.0.1', $DemoPort)
        if (-not $demoConnectTask.Wait(1000) -or -not $demoConnection.Connected) {
            return @{ State = 'free' }
        }
    }
    catch { return @{ State = 'free' } }
    finally { $demoConnection.Dispose() }

    try {
        $demoStatus = Invoke-RestMethod -Uri "http://127.0.0.1:$DemoPort/api/status" -TimeoutSec 15 -MaximumRedirection 0
    }
    catch {
        return @{ State = 'busy'; Reason = 'The port is occupied, but no healthy TraceJudge demo was detected.' }
    }
    if ($demoStatus.app.name -ne 'tracejudge-hy3' -or $demoStatus.modes.fixture.available -ne $true) {
        return @{ State = 'busy'; Reason = 'The port is occupied by another service.' }
    }
    if ($OfflineRequested) {
        if ($demoStatus.app.offline_mode -ne $true) {
            return @{ State = 'busy'; Reason = 'The existing demo was not confirmed to be started with -Offline.' }
        }
    }
    elseif ($demoStatus.app.offline_mode -eq $true -or $demoStatus.modes.hy3.available -ne $true) {
        return @{ State = 'busy'; Reason = 'The existing demo does not have Hy3 available (it may be an older offline service).' }
    }
    return @{ State = 'reuse' }
}

function Test-DemoDockerReady {
    param([string]$DockerExe, [int]$TimeoutMilliseconds = 10000)

    # Bound each probe as well as the overall startup wait. Never run a container.
    $demoProbe = New-Object System.Diagnostics.Process
    $demoProbe.StartInfo.FileName = $DockerExe
    $demoProbe.StartInfo.Arguments = 'info'
    $demoProbe.StartInfo.UseShellExecute = $false
    $demoProbe.StartInfo.CreateNoWindow = $true
    $demoProbe.StartInfo.RedirectStandardOutput = $true
    $demoProbe.StartInfo.RedirectStandardError = $true
    try {
        [void]$demoProbe.Start()
        $demoOutput = $demoProbe.StandardOutput.ReadToEndAsync()
        $demoError = $demoProbe.StandardError.ReadToEndAsync()
        if (-not $demoProbe.WaitForExit($TimeoutMilliseconds)) {
            $demoProbe.Kill()
            $demoProbe.WaitForExit()
            return $false
        }
        return ($demoProbe.ExitCode -eq 0)
    }
    catch { return $false }
    finally { $demoProbe.Dispose() }
}

function Initialize-DemoDocker {
    param([int]$WaitSeconds)

    $demoDockerRoots = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop'),
        (Join-Path $env:ProgramFiles 'Docker\Docker'),
        (Join-Path $env:LOCALAPPDATA 'Docker')
    )
    $demoDockerCommand = Get-Command docker.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($demoDockerCommand) {
        $demoDockerExe = $demoDockerCommand.Source
        $demoDockerRoots += Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $demoDockerExe))
    }
    else {
        $demoDockerExe = $demoDockerRoots |
            ForEach-Object { Join-Path $_ 'resources\bin\docker.exe' } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Select-Object -First 1
    }
    if (-not $demoDockerExe) {
        throw 'Docker CLI was not found. Install Docker Desktop, or use -Offline for a fixture demo.'
    }

    # Include per-user installations even when this terminal has an older PATH.
    # Only this launcher and its child processes receive the addition.
    $env:PATH = (Split-Path -Parent $demoDockerExe) + [IO.Path]::PathSeparator + $env:PATH
    if (Test-DemoDockerReady -DockerExe $demoDockerExe) {
        Write-Host 'Docker is ready; using the running engine.'
        return
    }

    $demoDesktop = $demoDockerRoots |
        ForEach-Object { Join-Path $_ 'Docker Desktop.exe' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
    if (-not $demoDesktop) {
        throw 'Docker is unavailable and Docker Desktop was not found. Start your Docker engine, or use -Offline.'
    }
    if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
        Write-Host 'Starting Docker Desktop...'
        Start-Process -FilePath $demoDesktop -WindowStyle Hidden | Out-Null
    }
    Write-Host "Waiting up to $WaitSeconds seconds for Docker. Complete any first-run setup in Docker Desktop if needed."
    $demoTimer = [Diagnostics.Stopwatch]::StartNew()
    $demoNextNotice = 10
    while ($demoTimer.Elapsed.TotalSeconds -lt $WaitSeconds) {
        $demoRemainingMs = [int][Math]::Ceiling(($WaitSeconds - $demoTimer.Elapsed.TotalSeconds) * 1000)
        if (Test-DemoDockerReady -DockerExe $demoDockerExe -TimeoutMilliseconds ([Math]::Min(10000, $demoRemainingMs))) {
            Write-Host 'Docker is ready.'
            return
        }
        if ($demoTimer.Elapsed.TotalSeconds -ge $demoNextNotice) {
            Write-Host ("Still waiting for Docker ({0}s)..." -f [int]$demoTimer.Elapsed.TotalSeconds)
            $demoNextNotice += 10
        }
        $demoSleepMs = [int][Math]::Min(2000, [Math]::Max(0, ($WaitSeconds - $demoTimer.Elapsed.TotalSeconds) * 1000))
        if ($demoSleepMs -gt 0) { Start-Sleep -Milliseconds $demoSleepMs }
    }
    throw "Docker did not become ready within $WaitSeconds seconds. Open Docker Desktop to check its status, then retry (optionally with -DockerWaitSeconds 240), or use -Offline."
}

$demoRoot = Split-Path -Parent $PSScriptRoot
$demoExisting = Get-DemoExistingService -DemoPort $Port -OfflineRequested ([bool]$Offline)
if ($demoExisting.State -eq 'reuse') {
    Write-Host "Demo is already running. Reusing the existing service on port $Port."
    Write-Host "Recording view: http://127.0.0.1:$Port/?recording=1"
    Write-Host 'No new server was started. Existing runs are preserved.'
    return
}
if ($demoExisting.State -eq 'busy') {
    throw "$($demoExisting.Reason) Stop the original service in its terminal, or select another port with -Port. No process was stopped."
}
$demoCandidates = @(
    (Join-Path $demoRoot '.venv\Scripts\python.exe'),
    (Join-Path $demoRoot 'artifacts\demo-preview-env\Scripts\python.exe')
)
$demoPython = $demoCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $demoPython) {
    $demoPython = (Get-Command python -ErrorAction Stop).Source
}

$demoOriginalPath = $env:PYTHONPATH
$demoOriginalSystemPath = $env:PATH
$demoOriginalOfflineMode = $env:TRACEJUDGE_DEMO_OFFLINE
# Python single quotes survive native argument passing in Windows PowerShell 5.1.
$demoOfflineBootstrap = "import os,runpy,sys; os.environ.update(dict.fromkeys(('HY3_BASE_URL','HY3_API_KEY','HY3_MODEL'),'')); sys.argv=sys.argv[1:]; runpy.run_module(sys.argv[0],run_name='__main__')"

Push-Location -LiteralPath $demoRoot
try {
    $env:PYTHONPATH = Join-Path $demoRoot 'src'
    $env:TRACEJUDGE_DEMO_OFFLINE = if ($Offline) { '1' } else { '0' }
    if (-not $Offline) {
        Initialize-DemoDocker -WaitSeconds $DockerWaitSeconds
    }
    if ($Offline) {
        # Set empty values inside Python: Windows PowerShell may remove empty env vars.
        & $demoPython -c $demoOfflineBootstrap tracejudge_hy3.demo_app.preflight
    }
    else {
        & $demoPython -m tracejudge_hy3.demo_app.preflight
    }
    if ($LASTEXITCODE -ne 0) {
        throw 'Demo readiness failed. Check Python dependencies and the messages above.'
    }
    Write-Host 'Starting the local UI. A pipeline runs only after you click Start.'
    if ($Offline) {
        & $demoPython -c $demoOfflineBootstrap tracejudge_hy3.demo_app.server --port $Port
    }
    else {
        & $demoPython -m tracejudge_hy3.demo_app.server --port $Port
    }
    if ($LASTEXITCODE -ne 0) { throw 'Demo server exited with an error.' }
}
finally {
    $env:PYTHONPATH = $demoOriginalPath
    $env:PATH = $demoOriginalSystemPath
    $env:TRACEJUDGE_DEMO_OFFLINE = $demoOriginalOfflineMode
    Pop-Location
}
