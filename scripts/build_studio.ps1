param(
    [switch]$Clean,
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project virtual environment not found: $Python"
}

& $Python -c "import PyInstaller, PySide6" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Studio packaging needs PyInstaller and PySide6. Run: .\.venv\Scripts\python.exe -m pip install -e '.[studio]' pyinstaller"
}

$DistChannel = if ($OneFile) { "dist\studio-portable" } else { "dist\studio" }
$DistPath = Join-Path $ProjectRoot $DistChannel
$WorkPath = Join-Path $ProjectRoot "build\pyinstaller-studio"
$Arguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--windowed",
    "--name", "File Indexer Studio",
    "--icon", (Join-Path $ProjectRoot "assets\file-indexer-studio.ico"),
    "--distpath", $DistPath,
    "--workpath", $WorkPath,
    "--specpath", (Join-Path $ProjectRoot "build"),
    "--collect-data", "mediaengine",
    "--collect-submodules", "mediaengine.plugins.builtin",
    "--hidden-import", "PySide6.QtMultimedia",
    "--hidden-import", "PySide6.QtMultimediaWidgets",
    "--copy-metadata", "mediaengine",
    "--add-data", ((Join-Path $ProjectRoot "qol_contract\change_sheet.toml") + ";share\mediaengine"),
    "--add-data", ((Join-Path $ProjectRoot "assets\file-indexer-studio.svg") + ";assets")
)

$PluginRoot = Join-Path $ProjectRoot "plugins-available"
$ExcludedPluginPath = '[\/](\.venv|venv|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|models|\.git)[\/]'
$ExcludedPluginFile = '\.(pyc|pyo|pt|bin|safetensors|onnx|caffemodel)$'
$PluginFiles = Get-ChildItem -LiteralPath $PluginRoot -Recurse -File | Where-Object {
    $_.FullName -notmatch $ExcludedPluginPath -and $_.Name -notmatch $ExcludedPluginFile
}
$StageRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "build\studio-plugin-bundle"))
$AllowedStageRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "build")) + [IO.Path]::DirectorySeparatorChar
if (-not $StageRoot.StartsWith($AllowedStageRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to stage plugins outside the build directory: $StageRoot"
}
if (Test-Path -LiteralPath $StageRoot) {
    Remove-Item -LiteralPath $StageRoot -Recurse -Force
}
$StagePluginRoot = Join-Path $StageRoot "plugins-available"
New-Item -ItemType Directory -Path $StagePluginRoot -Force | Out-Null
$PluginRootPrefix = $PluginRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
foreach ($PluginFile in $PluginFiles) {
    $Relative = $PluginFile.FullName.Substring($PluginRootPrefix.Length)
    $Destination = Join-Path $StagePluginRoot $Relative
    $DestinationDirectory = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $DestinationDirectory -Force | Out-Null
    Copy-Item -LiteralPath $PluginFile.FullName -Destination $Destination -Force
}
$Arguments += @("--add-data", ($StagePluginRoot + ";plugins-available"))

if ($Clean) { $Arguments += "--clean" }
if ($OneFile) { $Arguments += "--onefile" }
$Arguments += (Join-Path $ProjectRoot "File Indexer Studio.pyw")

& $Python @Arguments
if ($LASTEXITCODE -ne 0) {
    throw "Studio packaging failed with exit code $LASTEXITCODE"
}

$Executable = if ($OneFile) {
    Join-Path $DistPath "File Indexer Studio.exe"
} else {
    Join-Path $DistPath "File Indexer Studio\File Indexer Studio.exe"
}
Write-Host "Built: $Executable"
