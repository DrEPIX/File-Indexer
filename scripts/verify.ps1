param(
    [string]$PythonPath,
    [switch]$Quick,
    [switch]$FullTypeCheck,
    [switch]$Smoke
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

if (-not $PythonPath) {
    $PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw "Python interpreter not found: $PythonPath`nCreate .venv or pass -PythonPath."
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path

function Invoke-CheckedPython {
    param(
        [Parameter(Mandatory)]
        [string]$Label,
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    Write-Host ""
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $PythonPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Push-Location $ProjectRoot
try {
    Write-Host "MediaEngine verification" -ForegroundColor Green
    Write-Host "  Root:   $ProjectRoot"
    Write-Host "  Python: $PythonPath"

    Write-Host ""
    Write-Host "==> PowerShell syntax" -ForegroundColor Cyan
    $PowerShellErrors = @()
    foreach ($ScriptPath in Get-ChildItem -LiteralPath "scripts" -Filter "*.ps1" -File) {
        $Tokens = $null
        $Errors = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile(
            $ScriptPath.FullName,
            [ref]$Tokens,
            [ref]$Errors
        )
        foreach ($ParseError in $Errors) {
            $PowerShellErrors += "$($ScriptPath.Name):$($ParseError.Extent.StartLineNumber): $($ParseError.Message)"
        }
    }
    if ($PowerShellErrors.Count -gt 0) {
        throw "PowerShell syntax failed:`n$($PowerShellErrors -join "`n")"
    }
    Write-Host "PowerShell scripts parse cleanly."

    Invoke-CheckedPython "Dependency consistency" @("-m", "pip", "check")
    Invoke-CheckedPython "Extension and QoL contracts" @("scripts/validate_extensions.py")
    Invoke-CheckedPython "CLI import and parser smoke test" @("-m", "mediaengine", "--help")

    $Docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($Docker) {
        Write-Host ""
        Write-Host "==> Docker Compose configuration" -ForegroundColor Cyan
        $PreviousAuthToken = $env:MEDIAENGINE_AUTH_TOKEN
        $env:MEDIAENGINE_AUTH_TOKEN = "verification-only-token"
        try {
            & $Docker.Source compose -f "docker/compose.yaml" config --quiet
            if ($LASTEXITCODE -ne 0) {
                throw "base Docker Compose configuration is invalid"
            }
            & $Docker.Source compose -f "docker/compose.yaml" -f "docker/compose.gpu.yaml" config --quiet
            if ($LASTEXITCODE -ne 0) {
                throw "GPU Docker Compose configuration is invalid"
            }
        }
        finally {
            $env:MEDIAENGINE_AUTH_TOKEN = $PreviousAuthToken
        }
    }
    else {
        Write-Warning "Docker CLI not found; Compose configuration validation was skipped"
    }

    if (-not $Quick) {
        Invoke-CheckedPython "Bytecode compilation" @(
            "-m", "compileall", "-q", "mediaengine", "qol_contract/src",
            "plugins-available", "examples", "scripts"
        )
    }

    # pytest's configured testpaths intentionally target the core suite. An
    # explicit repository-wide run prevents the QoL and analyzer contract tests
    # from becoming an invisible second-class suite. Importlib mode also keeps
    # independent plugin test modules isolated from one another.
    Invoke-CheckedPython "Repository-wide tests" @(
        "-m", "pytest", "-q", "--import-mode=importlib",
        "tests", "qol_contract/tests", "plugins-available", "scripts"
    )

    $previousMypyPath = $env:MYPYPATH
    $env:MYPYPATH = (Resolve-Path -LiteralPath "qol_contract/src").Path
    try {
        # The contract layer intentionally treats application and optional
        # dependencies as opaque ports, so this check is stable on a minimal
        # Python install and still applies strict checks to every owned module.
        Invoke-CheckedPython "QoL and extension static analysis" @(
            "-m", "mypy", "--no-site-packages", "--ignore-missing-imports",
            "--follow-imports=skip", "qol_contract/src", "scripts/validate_extensions.py"
        )

        if ($FullTypeCheck) {
            Invoke-CheckedPython "Full project static analysis" @(
                "-m", "mypy", "mediaengine", "qol_contract/src"
            )
        }
    }
    finally {
        $env:MYPYPATH = $previousMypyPath
    }

    if ($Smoke) {
        Write-Host ""
        Write-Host "==> Disposable backend smoke test" -ForegroundColor Cyan
        & (Join-Path $PSScriptRoot "smoke_backend.ps1") -PythonPath $PythonPath
        if ($LASTEXITCODE -ne 0) {
            throw "Disposable backend smoke test failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host ""
    Write-Host "Verification passed." -ForegroundColor Green
}
finally {
    Pop-Location
}
