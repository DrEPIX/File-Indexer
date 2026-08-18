param([string]$Executable)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Executable) {
    $Executable = Join-Path $ProjectRoot "dist\studio\File Indexer Studio\File Indexer Studio.exe"
}
$Executable = (Resolve-Path -LiteralPath $Executable).Path
$Process = Start-Process -FilePath $Executable -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Seconds 8
    $Process.Refresh()
    if ($Process.HasExited) {
        throw "Studio exited during startup with code $($Process.ExitCode)"
    }
    Write-Host "Studio remained healthy through startup (PID $($Process.Id))."
}
finally {
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force
        $Process.WaitForExit()
    }
}
Write-Host "Packaged Studio smoke test passed."
