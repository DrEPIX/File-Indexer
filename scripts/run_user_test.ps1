param(
    [string]$LibraryPath = ".\library",
    [string]$DataPath = ".\data",
    [int]$Port = 8420
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$libraryInput = if ([System.IO.Path]::IsPathRooted($LibraryPath)) {
    $LibraryPath
} else {
    Join-Path $repoRoot $LibraryPath
}
$dataInput = if ([System.IO.Path]::IsPathRooted($DataPath)) {
    $DataPath
} else {
    Join-Path $repoRoot $DataPath
}
$library = [System.IO.Path]::GetFullPath($libraryInput)
$data = [System.IO.Path]::GetFullPath($dataInput)
$dbDir = Join-Path $data "db"
$derivatives = Join-Path $data "derivatives"
New-Item -ItemType Directory -Force -Path $library, $dbDir, $derivatives | Out-Null

$env:MEDIAENGINE__LIBRARY__ROOTS = ConvertTo-Json -Compress @($library)
$env:MEDIAENGINE__STORAGE__DB_PATH = Join-Path $dbDir "library.db"
$env:MEDIAENGINE__STORAGE__DERIVATIVES_PATH = $derivatives
$env:MEDIAENGINE__API__HOST = "127.0.0.1"
$env:MEDIAENGINE__API__PORT = [string]$Port
$env:MEDIAENGINE_QOL_SHEET = Join-Path $repoRoot "qol_contract\change_sheet.toml"

Write-Host "MediaEngine user-test server"
Write-Host "  Library: $library"
Write-Host "  API docs: http://127.0.0.1:$Port/docs"
Write-Host "  Health:   http://127.0.0.1:$Port/api/health"
Write-Host "Press Ctrl+C to stop."

Push-Location $repoRoot
try {
    & $python -m mediaengine.api --host 127.0.0.1 --port $Port
}
finally {
    Pop-Location
}
