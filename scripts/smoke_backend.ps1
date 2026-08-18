param(
    [string]$PythonPath,
    [switch]$KeepArtifacts,
    [switch]$SkipVideo,
    [ValidateRange(2, 1000)]
    [int]$DuplicateCount = 2
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python interpreter not found: $PythonPath"
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path

$TemporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$SmokeRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $TemporaryRoot ("mediaengine-smoke-" + [guid]::NewGuid().ToString("N")))
)
if (-not $SmokeRoot.StartsWith($TemporaryRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to create a smoke directory outside the system temporary directory: $SmokeRoot"
}

$Library = Join-Path $SmokeRoot "library"
$State = Join-Path $SmokeRoot "state"
$Derivatives = Join-Path $State "derivatives"
$Database = Join-Path $State "library.db"
New-Item -ItemType Directory -Force -Path $Library, $Derivatives | Out-Null

$PreviousEnvironment = @{}
$EnvironmentKeys = @(
    "MEDIAENGINE__LIBRARY__ROOTS",
    "MEDIAENGINE__STORAGE__DB_PATH",
    "MEDIAENGINE__STORAGE__DERIVATIVES_PATH",
    "MEDIAENGINE_SMOKE_IMAGE"
)
foreach ($Key in $EnvironmentKeys) {
    $PreviousEnvironment[$Key] = [Environment]::GetEnvironmentVariable($Key, "Process")
}

function Invoke-MediaEngineJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    # The CLI deliberately emits structured operational logs on stderr. Under
    # PowerShell's strict error handling those native stderr records can become
    # terminating errors even when the command succeeds, so keep the JSON-only
    # stdout channel separate and surface stderr only for an actual failure.
    $StderrPath = Join-Path $State ("cli-" + [guid]::NewGuid().ToString("N") + ".stderr.log")
    $PreviousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $Output = & $PythonPath -m mediaengine --json @Arguments 2> $StderrPath
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    if ($ExitCode -ne 0) {
        $FailureDetails = if (Test-Path -LiteralPath $StderrPath) {
            (Get-Content -LiteralPath $StderrPath -Tail 20) -join [Environment]::NewLine
        }
        else {
            "No stderr output was captured."
        }
        throw "mediaengine $($Arguments -join ' ') failed with exit code $ExitCode`n$FailureDetails"
    }
    return ($Output | Out-String | ConvertFrom-Json)
}

function Assert-Smoke {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) {
        throw "Smoke assertion failed: $Message"
    }
}

try {
    Write-Host "Creating disposable media library: $Library"
    $ImagePath = Join-Path $Library "sunset image.jpg"
    $env:MEDIAENGINE_SMOKE_IMAGE = $ImagePath
    $ImageScript = 'from PIL import Image; import os; Image.new("RGB", (96, 64), (214, 112, 72)).save(os.environ["MEDIAENGINE_SMOKE_IMAGE"], format="JPEG", quality=90)'
    $EncodedImageScript = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ImageScript))
    $ImageLauncher = "import base64;exec(base64.b64decode('$EncodedImageScript'))"
    & $PythonPath -c $ImageLauncher
    if ($LASTEXITCODE -ne 0) {
        throw "could not create JPEG fixture"
    }
    Copy-Item -LiteralPath $ImagePath -Destination (Join-Path $Library "sunset duplicate.jpg")
    for ($DuplicateIndex = 3; $DuplicateIndex -le $DuplicateCount; $DuplicateIndex++) {
        Copy-Item -LiteralPath $ImagePath -Destination (
            Join-Path $Library ("sunset duplicate {0:D4}.jpg" -f $DuplicateIndex)
        )
    }
    $UnicodeLine = "Unicode survives indexing: caf$([char]0x00E9), $([char]0x6771)$([char]0x4EAC), na$([char]0x00EF)ve."
    Set-Content -LiteralPath (Join-Path $Library "research notes.txt") -Encoding UTF8 -Value @(
        "MediaEngine smoke-test document",
        $UnicodeLine,
        "The searchable token is quartz-platypus."
    )
    $LongDocumentName = "long-" + ("x" * 140) + ".txt"
    Set-Content -LiteralPath (Join-Path $Library $LongDocumentName) -Encoding UTF8 -Value @(
        "Long-filename indexing fixture",
        "The searchable token is velvet-astronomy."
    )
    [System.IO.File]::WriteAllBytes(
        (Join-Path $Library "empty file.bin"),
        [byte[]]::new(0)
    )

    $VideoCreated = $false
    if (-not $SkipVideo) {
        $Ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
        if ($Ffmpeg) {
            $FfmpegArguments = @(
                "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "color=c=0x345f8c:s=96x64:d=1", "-pix_fmt", "yuv420p",
                (Join-Path $Library "one second sample.mp4")
            )
            & $Ffmpeg.Source @FfmpegArguments
            if ($LASTEXITCODE -ne 0) {
                throw "ffmpeg failed to create the video fixture"
            }
            $VideoCreated = $true
        }
        else {
            Write-Warning "ffmpeg is unavailable; video smoke coverage was skipped"
        }
    }

    $env:MEDIAENGINE__LIBRARY__ROOTS = ConvertTo-Json -Compress @($Library)
    $env:MEDIAENGINE__STORAGE__DB_PATH = $Database
    $env:MEDIAENGINE__STORAGE__DERIVATIVES_PATH = $Derivatives

    Write-Host "Scanning mixed media and generating derivatives..."
    $First = @(Invoke-MediaEngineJson scan $Library)
    $ExpectedSeen = $DuplicateCount + 2 + $(if ($VideoCreated) { 1 } else { 0 })
    $ExpectedAssets = if ($VideoCreated) { 4 } else { 3 }
    Assert-Smoke ($First.Count -eq 1) "one root result was expected"
    Assert-Smoke ($First[0].files_seen -ge $ExpectedSeen) "all supported fixture files should be seen"
    Assert-Smoke ($First[0].assets_created -ge $ExpectedAssets) "duplicate bytes should collapse while distinct files index"
    Assert-Smoke ($First[0].files_failed -eq 0) "the first scan should not fail any file"
    $DerivativeWarnings = @(
        $First[0].warnings | Where-Object { [string]$_ -like "derivative generation failed*" }
    )

    $StatsBeforeRename = Invoke-MediaEngineJson stat
    Assert-Smoke ($StatsBeforeRename.database.assets -ge $ExpectedAssets) "stats should report indexed assets"
    Assert-Smoke ($StatsBeforeRename.database.files -ge $ExpectedSeen) "stats should report every supported path"
    if ($VideoCreated) {
        Assert-Smoke ($StatsBeforeRename.assets_by_type.video -eq 1) "the generated MP4 should be a video"
    }

    $ImageSearch = Invoke-MediaEngineJson search "type:image"
    Assert-Smoke ($ImageSearch.total -eq 1) "byte-identical JPEG copies should resolve to one image asset"
    $TextSearch = Invoke-MediaEngineJson search "quartz-platypus"
    Assert-Smoke ($TextSearch.total -eq 1) "document text should be searchable"
    $LongNameSearch = Invoke-MediaEngineJson search "velvet-astronomy"
    Assert-Smoke ($LongNameSearch.total -eq 1) "long-filename document text should be searchable"

    Write-Host "Checking idempotent repeat scan..."
    $Second = @(Invoke-MediaEngineJson scan $Library)
    Assert-Smoke ($Second[0].assets_created -eq 0) "repeat scan must not create assets"
    Assert-Smoke ($Second[0].files_failed -eq 0) "repeat scan should not fail"
    Assert-Smoke ($Second[0].files_skipped -ge $ExpectedSeen) "unchanged paths should use the triage fast path"

    Write-Host "Checking content identity across a rename..."
    $OriginalDuplicate = Join-Path $Library "sunset duplicate.jpg"
    $RenamedDuplicate = Join-Path $Library ("renamed-" + [char]0x6771 + [char]0x4EAC + ".jpg")
    Move-Item -LiteralPath $OriginalDuplicate -Destination $RenamedDuplicate
    $Third = @(Invoke-MediaEngineJson scan $Library)
    $StatsAfterRename = Invoke-MediaEngineJson stat
    Assert-Smoke ($Third[0].assets_created -eq 0) "renaming existing bytes must not create an asset"
    $AssetCountStayedStable = $StatsAfterRename.database.assets -eq $StatsBeforeRename.database.assets
    Assert-Smoke $AssetCountStayedStable "asset count must remain stable after a rename"

    $Doctor = Invoke-MediaEngineJson doctor
    Assert-Smoke ($Doctor.integrity[0] -eq "ok") "SQLite integrity_check should pass"
    Assert-Smoke ($DerivativeWarnings.Count -eq 0) "duplicate files must not race while publishing one content-addressed derivative"

    Write-Host "Backend smoke test passed." -ForegroundColor Green
    Write-Host "  Assets:      $($StatsAfterRename.database.assets)"
    Write-Host "  File rows:   $($StatsAfterRename.database.files)"
    Write-Host "  Derivatives: $($StatsAfterRename.derivatives.total_bytes) bytes"
}
finally {
    foreach ($Key in $EnvironmentKeys) {
        [Environment]::SetEnvironmentVariable($Key, $PreviousEnvironment[$Key], "Process")
    }
    if ($KeepArtifacts) {
        Write-Host "Smoke artifacts retained at: $SmokeRoot"
    }
    elseif (Test-Path -LiteralPath $SmokeRoot) {
        $ResolvedSmokeRoot = [System.IO.Path]::GetFullPath($SmokeRoot)
        if (
            $ResolvedSmokeRoot -eq $TemporaryRoot -or
            -not $ResolvedSmokeRoot.StartsWith($TemporaryRoot, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "Refusing unsafe smoke cleanup target: $ResolvedSmokeRoot"
        }
        Remove-Item -LiteralPath $ResolvedSmokeRoot -Recurse -Force
    }
}
