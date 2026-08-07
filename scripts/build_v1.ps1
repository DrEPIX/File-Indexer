param(
    [switch]$Clean
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

$Arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--windowed",
    "--name", "File Indexer V1",
    "--distpath", (Join-Path $ProjectRoot "dist"),
    "--workpath", (Join-Path $ProjectRoot "build\pyinstaller-v1"),
    "--specpath", (Join-Path $ProjectRoot "build"),
    "--collect-data", "mediaengine",
    "--collect-submodules", "mediaengine.plugins.builtin",
    "--copy-metadata", "mediaengine",
    (Join-Path $ProjectRoot "File Indexer V1.pyw")
)

if ($Clean) {
    $Arguments = $Arguments[0..2] + "--clean" + $Arguments[3..($Arguments.Count - 1)]
}

& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "V1 packaging failed with exit code $LASTEXITCODE"
}

$Executable = Join-Path $ProjectRoot "dist\File Indexer V1\File Indexer V1.exe"
Write-Host "Built: $Executable"
