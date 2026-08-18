param(
    [string]$ExecutablePath,
    [string]$PythonPath,
    [ValidateRange(1, 30)]
    [int]$StartupSeconds = 5,
    [switch]$SkipFreshnessCheck,
    [switch]$KeepArtifacts
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ExecutablePath) {
    $ExecutablePath = Join-Path $ProjectRoot "dist\release\File Indexer V1\File Indexer V1.exe"
}
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
foreach ($RequiredFile in @($ExecutablePath, $PythonPath)) {
    if (-not (Test-Path -LiteralPath $RequiredFile -PathType Leaf)) {
        throw "Required packaged-app smoke file not found: $RequiredFile"
    }
}
$ExecutablePath = (Resolve-Path -LiteralPath $ExecutablePath).Path
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path

if (-not $SkipFreshnessCheck) {
    $SourceFiles = @(
        Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "mediaengine") -Recurse -File -Filter "*.py"
        Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "qol_contract\src") -Recurse -File -Filter "*.py"
        Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "plugins-available") -Recurse -File |
            Where-Object {
                $_.FullName -notmatch '[\\/](\.venv|venv|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|models|\.git)[\\/]'
            }
        Get-Item -LiteralPath (Join-Path $ProjectRoot "File Indexer V1.pyw")
    )
    $NewestSource = $SourceFiles | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    $Executable = Get-Item -LiteralPath $ExecutablePath
    if ($NewestSource -and $Executable.LastWriteTimeUtc -lt $NewestSource.LastWriteTimeUtc) {
        throw "Frozen app is stale; rebuild after changing $($NewestSource.FullName)"
    }

    # Onedir builds expose bundled data for a direct reproducibility check.
    # Onefile builds unpack it at runtime, so freshness is covered by the EXE
    # timestamp plus the two-launch probe below.
    $PackagedSheet = Join-Path (Split-Path -Parent $ExecutablePath) "_internal\share\mediaengine\change_sheet.toml"
    if (Test-Path -LiteralPath (Join-Path (Split-Path -Parent $ExecutablePath) "_internal")) {
        $SourceSheet = Join-Path $ProjectRoot "qol_contract\change_sheet.toml"
        if (-not (Test-Path -LiteralPath $PackagedSheet -PathType Leaf)) {
            throw "Frozen app is missing its QoL change sheet: $PackagedSheet"
        }
        if (
            (Get-FileHash -LiteralPath $SourceSheet -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $PackagedSheet -Algorithm SHA256).Hash
        ) {
            throw "Frozen app contains a stale QoL change sheet; rebuild the release"
        }
    }
}

$TemporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$SmokeRoot = [IO.Path]::GetFullPath(
    (Join-Path $TemporaryRoot ("mediaengine-packaged-smoke-" + [guid]::NewGuid().ToString("N")))
)
if (-not $SmokeRoot.StartsWith($TemporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create packaged-app smoke state outside the temporary directory"
}
New-Item -ItemType Directory -Force -Path $SmokeRoot | Out-Null

function Invoke-StartupProbe {
    param([Parameter(Mandatory)][string]$Label)

    $Process = Start-Process -FilePath $ExecutablePath -PassThru -WindowStyle Hidden
    if ($Process.WaitForExit($StartupSeconds * 1000)) {
        throw "$Label exited during startup with code $($Process.ExitCode)"
    }
    Write-Host "$Label remained healthy through startup (PID $($Process.Id))."

    # A PyInstaller onefile bootloader owns a same-name child that runs the
    # application. Capture descendants before stopping the parent so the probe
    # cannot leave that child holding the database or log open.
    $ProcessTree = @($Process.Id)
    do {
        $Added = $false
        foreach ($Candidate in Get-CimInstance Win32_Process) {
            if (
                $Candidate.ParentProcessId -in $ProcessTree -and
                $Candidate.ProcessId -notin $ProcessTree
            ) {
                $ProcessTree += [int]$Candidate.ProcessId
                $Added = $true
            }
        }
    } while ($Added)
    [array]::Reverse($ProcessTree)
    foreach ($ProcessId in $ProcessTree) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    $Process.WaitForExit()
    Start-Sleep -Milliseconds 250
}

$PreviousLocalAppData = $env:LOCALAPPDATA
$SmokeDatabase = Join-Path $SmokeRoot "File Indexer V1\data\library.db"
try {
    $env:LOCALAPPDATA = $SmokeRoot
    Invoke-StartupProbe "Initial frozen-app launch"
    Invoke-StartupProbe "Recovery launch after forced shutdown"

    if (-not (Test-Path -LiteralPath $SmokeDatabase -PathType Leaf)) {
        throw "Frozen app did not create its database in isolated LocalAppData"
    }
    $PreviousSmokeDatabase = $env:MEDIAENGINE_PACKAGED_SMOKE_DB
    $env:MEDIAENGINE_PACKAGED_SMOKE_DB = $SmokeDatabase
    try {
        $Integrity = & $PythonPath -c (
            "import os,sqlite3; " +
            "c=sqlite3.connect(os.environ['MEDIAENGINE_PACKAGED_SMOKE_DB']); " +
            "print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
        )
        if ($LASTEXITCODE -ne 0 -or ($Integrity | Out-String).Trim() -ne "ok") {
            throw "Frozen-app database integrity check failed: $Integrity"
        }
    }
    finally {
        $env:MEDIAENGINE_PACKAGED_SMOKE_DB = $PreviousSmokeDatabase
    }

    $LogPath = Join-Path $SmokeRoot "File Indexer V1\data\engine.log"
    if (Test-Path -LiteralPath $LogPath) {
        $FatalLines = @(Get-Content -LiteralPath $LogPath | Where-Object {
            $_ -match '\s(ERROR|CRITICAL)\s'
        })
        if ($FatalLines.Count -gt 0) {
            throw "Frozen app logged startup errors:`n$($FatalLines -join "`n")"
        }
    }
    Write-Host "Packaged application smoke test passed." -ForegroundColor Green
}
finally {
    $env:LOCALAPPDATA = $PreviousLocalAppData
    if ($KeepArtifacts) {
        Write-Host "Packaged-app smoke artifacts retained at: $SmokeRoot"
    }
    elseif (Test-Path -LiteralPath $SmokeRoot) {
        $ResolvedSmokeRoot = [IO.Path]::GetFullPath($SmokeRoot)
        if (
            $ResolvedSmokeRoot -eq $TemporaryRoot -or
            -not $ResolvedSmokeRoot.StartsWith($TemporaryRoot, [StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "Refusing unsafe packaged-app smoke cleanup target: $ResolvedSmokeRoot"
        }
        for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
            try {
                Remove-Item -LiteralPath $ResolvedSmokeRoot -Recurse -Force
                break
            }
            catch {
                if ($Attempt -eq 5) { throw }
                Start-Sleep -Milliseconds 250
            }
        }
    }
}
