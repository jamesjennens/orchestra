<#
.SYNOPSIS
Poll for work and wake this lane's harness, once per new fingerprint.

.DESCRIPTION
An EXAMPLE waker from the Orchestra kit (tools/wake), for Windows PowerShell 5.1
or newer. It runs `client.py wait --since FINGERPRINT`, keeps the last fingerprint
in a state file beside it, and when the wait says something is this lane's move it
runs ONE wake command - the setting that starts a new turn of the harness this lane
runs in. The kit cannot know how a harness is woken, so the lane fills that one
command in.

The rules it keeps:
  * one wake per new fingerprint: the fingerprint is written to the state file
    BEFORE the wake command runs, so a restart or a second Task Scheduler trigger
    cannot run the command twice for the same fingerprint;
  * one instance at a time: a lock file holding this process id, so a trigger that
    fires while a previous run still polls exits instead of polling twice;
  * bounded: client.py wait is given --timeout, so one run ends by itself;
  * one log line per wake, and nothing else in the wake log;
  * it reads no secret: the client config is the only credential input, and the
    wake command is the lane's own setting;
  * it never runs the work: it only invokes the wake command and exits.

Run it from Task Scheduler at logon and every N minutes (README.md in this
directory has the exact steps and commands). Exit codes: 0 the run is over
normally (a wake, a timeout, or another instance is already running); 1 a setting
is unusable, the state file is not a fingerprint, or the wake command failed; 2 the
client refused the wait; 3 the client could not be reached or did not answer a
wait read.
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [string]$Client = '',
    [string]$Config = '',
    [string]$Project = '',
    [string]$Actor = '',
    [string]$Wake = '',
    [int]$Timeout = 0,
    [int]$Interval = 0,
    [string]$State = '',
    [string]$Lock = '',
    [string]$Log = '',
    [string]$EnvFile = '',
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

function Show-Usage {
    Write-Output @'
Usage: wake.ps1 [SETTINGS...]

Poll client.py wait and run ONE wake command when something is this lane's move.

Settings (a parameter wins over the environment, which wins over the env file):
  -Client FILE        client.py from this kit (WAKE_CLIENT)
  -Config FILE        this lane's private client configuration (WAKE_CONFIG)
  -Project NAME       project id (WAKE_PROJECT)
  -Actor ACTOR        this lane's saved actor (WAKE_ACTOR)
  -Wake COMMAND       the one command that starts a new turn of this lane's
                      harness; run once per new fingerprint with cmd.exe /c
                      (WAKE_COMMAND)
  -Python COMMAND     interpreter that runs client.py; default python
                      (WAKE_PYTHON)
  -Timeout SECONDS    bound on one wait, given to client.py wait; default 3600
                      (WAKE_TIMEOUT)
  -Interval SECONDS   poll interval, given to client.py wait; default is the
                      client's own 300 (WAKE_INTERVAL)
  -State FILE         last fingerprint; default wake.state beside this script
                      (WAKE_STATE)
  -Lock FILE          one-instance lock; default wake.lock beside this script
                      (WAKE_LOCK)
  -Log FILE           one line per wake; default wake.log beside this script
                      (WAKE_LOG)
  -EnvFile FILE       NAME=value settings, WAKE_* names only, read before the
                      parameters; a file with spaces in the wake command needs no
                      Task Scheduler quoting this way
  -Help               this text

Exit codes: 0 normal end, 1 setting/state/wake failure, 2 the client refused,
3 the client could not be reached. See README.md for Task Scheduler at logon
and every N minutes.
'@
}

if ($Help) {
    Show-Usage
    exit 0
}

$here = $PSScriptRoot
if (-not $here) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
}

# Settings from the environment first, then the env file, then a parameter, then the
# default. The env file is NAME=value lines: a value may be wrapped in one pair of
# double quotes, and everything else about it is literal (no PowerShell is run).
$fromEnv = @{}
foreach ($name in @('WAKE_PYTHON', 'WAKE_CLIENT', 'WAKE_CONFIG', 'WAKE_PROJECT', 'WAKE_ACTOR',
                    'WAKE_COMMAND', 'WAKE_TIMEOUT', 'WAKE_INTERVAL', 'WAKE_STATE', 'WAKE_LOCK', 'WAKE_LOG')) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($value) { $fromEnv[$name] = $value }
}
if ($EnvFile) {
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        [Console]::Error.WriteLine("wake.ps1: no environment file: $EnvFile")
        exit 2
    }
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        $text = ([string]$line).Trim()
        if (-not $text -or $text.StartsWith('#') -or -not $text.Contains('=')) {
            continue
        }
        $at = $text.IndexOf('=')
        $name = $text.Substring(0, $at).Trim()
        $value = $text.Substring($at + 1).Trim()
        if ($value.Length -ge 2 -and $value.StartsWith('"') -and $value.EndsWith('"')) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $fromEnv[$name] = $value
    }
}

if (-not $Python -and $fromEnv.ContainsKey('WAKE_PYTHON')) { $Python = $fromEnv['WAKE_PYTHON'] }
if (-not $Python) { $Python = 'python' }
if (-not $Client -and $fromEnv.ContainsKey('WAKE_CLIENT')) { $Client = $fromEnv['WAKE_CLIENT'] }
if (-not $Config -and $fromEnv.ContainsKey('WAKE_CONFIG')) { $Config = $fromEnv['WAKE_CONFIG'] }
if (-not $Project -and $fromEnv.ContainsKey('WAKE_PROJECT')) { $Project = $fromEnv['WAKE_PROJECT'] }
if (-not $Actor -and $fromEnv.ContainsKey('WAKE_ACTOR')) { $Actor = $fromEnv['WAKE_ACTOR'] }
if (-not $Wake -and $fromEnv.ContainsKey('WAKE_COMMAND')) { $Wake = $fromEnv['WAKE_COMMAND'] }
if ($Timeout -le 0 -and $fromEnv.ContainsKey('WAKE_TIMEOUT')) {
    $parsedTimeout = 0
    if ([int]::TryParse([string]$fromEnv['WAKE_TIMEOUT'], [ref]$parsedTimeout)) { $Timeout = $parsedTimeout }
}
if ($Timeout -le 0) { $Timeout = 3600 }
if ($Interval -le 0 -and $fromEnv.ContainsKey('WAKE_INTERVAL')) {
    $parsedInterval = 0
    if ([int]::TryParse([string]$fromEnv['WAKE_INTERVAL'], [ref]$parsedInterval)) { $Interval = $parsedInterval }
}
if (-not $State -and $fromEnv.ContainsKey('WAKE_STATE')) { $State = $fromEnv['WAKE_STATE'] }
if (-not $State) { $State = Join-Path $here 'wake.state' }
if (-not $Lock -and $fromEnv.ContainsKey('WAKE_LOCK')) { $Lock = $fromEnv['WAKE_LOCK'] }
if (-not $Lock) { $Lock = Join-Path $here 'wake.lock' }
if (-not $Log -and $fromEnv.ContainsKey('WAKE_LOG')) { $Log = $fromEnv['WAKE_LOG'] }
if (-not $Log) { $Log = Join-Path $here 'wake.log' }

if (-not $Client) { [Console]::Error.WriteLine('wake.ps1: -Client FILE (client.py from this kit) is required'); exit 2 }
if (-not $Config) { [Console]::Error.WriteLine("wake.ps1: -Config FILE (this lane's client config) is required"); exit 2 }
if (-not $Project) { [Console]::Error.WriteLine('wake.ps1: -Project NAME is required'); exit 2 }
if (-not $Actor) { [Console]::Error.WriteLine('wake.ps1: -Actor ACTOR is required'); exit 2 }
if (-not $Wake) { [Console]::Error.WriteLine('wake.ps1: -Wake COMMAND is required: it is the one thing the kit cannot know'); exit 2 }
if ($Wake -match '^REPLACE_') { [Console]::Error.WriteLine('wake.ps1: -Wake is still the template placeholder: fill in the command that starts a new turn of this lane'); exit 2 }
if (-not (Test-Path -LiteralPath $Client -PathType Leaf)) { [Console]::Error.WriteLine("wake.ps1: no client.py at $Client"); exit 2 }
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { [Console]::Error.WriteLine("wake.ps1: no client config at $Config"); exit 2 }

foreach ($parent in @((Split-Path -Parent $State), (Split-Path -Parent $Lock), (Split-Path -Parent $Log))) {
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
}

function Enter-WakeLock {
    param([string]$Path)
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew,
                                          [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    } catch [System.IO.IOException] {
        $held = $null
        try {
            $held = [string](Get-Content -LiteralPath $Path -TotalCount 1)
        } catch {
            # The file is held open by a running instance: do not steal it.
            return $null
        }
        $held = $held.Trim()
        if ($held -notmatch '^\d+$') {
            # Another instance is between its create and its write: leave it alone.
            return $null
        }
        $running = $false
        try {
            $null = Get-Process -Id ([int]$held) -ErrorAction Stop
            $running = $true
        } catch {
            $running = $false
        }
        if ($running) {
            return $null
        }
        Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        try {
            $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew,
                                              [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        } catch {
            return $null
        }
    }
    $writer = New-Object System.IO.StreamWriter($stream)
    $writer.WriteLine([string]$PID)
    $writer.Flush()
    return @{ Writer = $writer; Path = $Path }
}

function Exit-WakeLock {
    param($Handle)
    if ($null -eq $Handle) { return }
    try { $Handle.Writer.Dispose() } catch { }
    Remove-Item -LiteralPath $Handle.Path -Force -ErrorAction SilentlyContinue
}

$lockHandle = Enter-WakeLock -Path $Lock
if ($null -eq $lockHandle) {
    # Another run is polling already; a second poll would only double the reads.
    exit 0
}

$exitCode = 0
try {
    $last = ''
    if (Test-Path -LiteralPath $State) {
        $last = ([string](Get-Content -LiteralPath $State -TotalCount 1)).Trim()
    }
    if ($last -and $last -notmatch '^wait1:[0-9a-f]{64}$') {
        [Console]::Error.WriteLine("wake.ps1: $State does not hold a wait fingerprint; move it away and let the waker run again")
        $exitCode = 1
        throw 'stop'
    }

    $clientArgs = @('--config', $Config, '--project', $Project, '--actor', $Actor, 'wait')
    if ($last) { $clientArgs += @('--since', $last) }
    $clientArgs += @('--timeout', [string]$Timeout)
    if ($Interval -gt 0) { $clientArgs += @('--interval', [string]$Interval) }

    $errFile = [System.IO.Path]::GetTempFileName()
    $global:LASTEXITCODE = 0
    try {
        $output = (& $Python $Client @clientArgs 2> $errFile | Out-String)
        $code = $LASTEXITCODE
    } catch {
        [Console]::Error.WriteLine("wake.ps1: could not run ${Python} ${Client}: $($_.Exception.Message)")
        Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
        $exitCode = 3
        throw 'stop'
    }

    $detail = ''
    if (Test-Path -LiteralPath $errFile) {
        try { $detail = ([string](Get-Content -LiteralPath $errFile -Raw)).Trim() } catch { }
        Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue
    }

    if ($code -eq 1) {
        # client.py wait's timeout: the bound was reached, nothing is the lane's move.
        $exitCode = 0
        throw 'stop'
    }
    if ($code -ne 0) {
        $lastLine = ''
        if ($detail) {
            $lines = $detail -split "`r?`n"
            $lastLine = [string]$lines[$lines.Length - 1]
        }
        [Console]::Error.WriteLine("wake.ps1: client.py wait exited $code" + $(if ($lastLine) { ": $lastLine" } else { '' }))
        $exitCode = $code
        throw 'stop'
    }

    $view = $null
    try {
        $view = $output | ConvertFrom-Json
    } catch {
        [Console]::Error.WriteLine('wake.ps1: client.py wait exited 0 but printed no wait answer')
        $exitCode = 3
        throw 'stop'
    }
    if ($null -eq $view -or $null -eq $view.fingerprint) {
        [Console]::Error.WriteLine('wake.ps1: client.py wait exited 0 but printed no wait answer')
        $exitCode = 3
        throw 'stop'
    }
    $fingerprint = [string]$view.fingerprint
    if ($fingerprint -notmatch '^wait1:[0-9a-f]{64}$') {
        [Console]::Error.WriteLine('wake.ps1: client.py wait printed a fingerprint this kit does not write')
        $exitCode = 3
        throw 'stop'
    }
    $itemCount = 0
    if ($null -ne $view.items) { $itemCount = @($view.items).Count }

    if ($last -and $fingerprint -eq $last) {
        # The same state the lane already acted on: never a second wake for it.
        $exitCode = 0
        throw 'stop'
    }

    # Record the new fingerprint BEFORE waking: a crash after the wake must not run
    # the command a second time for the same state.
    [System.IO.File]::WriteAllText($State, $fingerprint + "`n", (New-Object System.Text.UTF8Encoding($false)))

    if ($itemCount -eq 0) {
        # The state changed but nothing is the lane's move now: do not wake it to
        # find nothing.
        $exitCode = 0
        throw 'stop'
    }

    $env:WAKE_FINGERPRINT = $fingerprint
    $env:WAKE_ITEMS = [string]$itemCount
    & cmd.exe /d /c $Wake
    $wakeCode = $LASTEXITCODE
    $result = 'ok'
    if ($wakeCode -ne 0) { $result = 'failed' }
    $stamp = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    Add-Content -LiteralPath $Log -Value "$stamp wake fingerprint=$fingerprint items=$itemCount result=$result" -Encoding UTF8
    if ($wakeCode -ne 0) {
        [Console]::Error.WriteLine('wake.ps1: the wake command failed. The new fingerprint is recorded, so it will not run again for this state; fix and test the command once')
        $exitCode = 1
        throw 'stop'
    }
} catch {
    if ($_.Exception.Message -ne 'stop') {
        [Console]::Error.WriteLine("wake.ps1: $($_.Exception.Message)")
        $exitCode = 1
    }
} finally {
    Exit-WakeLock -Handle $lockHandle
}

exit $exitCode
