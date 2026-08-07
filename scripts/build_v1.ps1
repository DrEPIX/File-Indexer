param(
    [switch]$Clean,
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Project virtual environment not found: $Python"
}

& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Run: .\.venv\Scripts\python.exe -m pip install pyinstaller"
}

$DistPath = if ($OneFile) {
    Join-Path $ProjectRoot "dist\portable"
} else {
    Join-Path $ProjectRoot "dist"
}

$Arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--windowed",
    "--name", "File Indexer V1",
    "--distpath", $DistPath,
    "--workpath", (Join-Path $ProjectRoot "build\pyinstaller-v1"),
    "--specpath", (Join-Path $ProjectRoot "build"),
    "--collect-data", "mediaengine",
    "--collect-submodules", "mediaengine.plugins.builtin",
    "--copy-metadata", "mediaengine"
)

if ($Clean) {
    $Arguments += "--clean"
}
if ($OneFile) {
    $Arguments += "--onefile"
}
$Arguments += (Join-Path $ProjectRoot "File Indexer V1.pyw")

& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "V1 packaging failed with exit code $LASTEXITCODE"
}

$Executable = if ($OneFile) {
    Join-Path $DistPath "File Indexer V1.exe"
} else {
    Join-Path $DistPath "File Indexer V1\File Indexer V1.exe"
}
Write-Host "Built: $Executable"
