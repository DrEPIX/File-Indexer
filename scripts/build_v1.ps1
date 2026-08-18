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

# Keep the installed onedir payload and portable single executable in separate
# release channels so either can be rebuilt without clobbering the other.
$DistChannel = if ($OneFile) { "dist\portable" } else { "dist\release" }
$DistPath = Join-Path $ProjectRoot $DistChannel

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
    "--copy-metadata", "mediaengine",
    "--add-data", ((Join-Path $ProjectRoot "qol_contract\change_sheet.toml") + ";share\mediaengine")
)

# Bundle deployable plugin examples without accidentally shipping local model
# weights, virtual environments, or Python caches. Copying the directory as a
# single --add-data tree previously pulled in >9,000 development files and
# made COLLECT fail on Windows.
$PluginRoot = Join-Path $ProjectRoot "plugins-available"
$ExcludedPluginPath = '[\\/](\.venv|venv|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|models|\.git)[\\/]'
$ExcludedPluginFile = '\.(pyc|pyo|pt|bin|safetensors|onnx|caffemodel)$'
$PluginFiles = Get-ChildItem -LiteralPath $PluginRoot -Recurse -File | Where-Object {
    $_.FullName -notmatch $ExcludedPluginPath -and $_.Name -notmatch $ExcludedPluginFile
}
$PluginRootPrefix = $PluginRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
foreach ($PluginFile in $PluginFiles) {
    $Relative = $PluginFile.FullName.Substring($PluginRootPrefix.Length)
    $RelativeDirectory = Split-Path -Parent $Relative
    $Destination = if ($RelativeDirectory) {
        Join-Path "plugins-available" $RelativeDirectory
    } else {
        "plugins-available"
    }
    $Arguments += @("--add-data", ($PluginFile.FullName + ";" + $Destination))
}

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
