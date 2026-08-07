param(
    [switch]$Clean,
    [switch]$SkipAppBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PayloadSource = Join-Path $ProjectRoot "dist\release\File Indexer V1"
$OutputDirectory = Join-Path $ProjectRoot "dist\installer"
$Intermediate = Join-Path $ProjectRoot "build\installer"
$Payload = Join-Path $Intermediate "payload"
$ToolsDirectory = Join-Path $ProjectRoot ".tools\wix3141"
$Archive = Join-Path $ProjectRoot ".tools\wix314-binaries.zip"
$WixUrl = "https://github.com/wixtoolset/wix3/releases/download/wix3141rtm/wix314-binaries.zip"
$WixSha256 = "6AC824E1642D6F7277D0ED7EA09411A508F6116BA6FAE0AA5F2C7DAA2FF43D31"

function Install-WixToolset {
    $Candle = Join-Path $ToolsDirectory "candle.exe"
    if (Test-Path -LiteralPath $Candle -PathType Leaf) {
        return
    }
    $ToolsRoot = Split-Path -Parent $ToolsDirectory
    New-Item -ItemType Directory -Force -Path $ToolsRoot, $ToolsDirectory | Out-Null
    Write-Host "Downloading WiX Toolset 3.14.1..."
    Invoke-WebRequest -Uri $WixUrl -OutFile $Archive
    if ($WixSha256) {
        $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash
        if ($Actual -ne $WixSha256) {
            throw "WiX archive hash mismatch: $Actual"
        }
    }
    Expand-Archive -LiteralPath $Archive -DestinationPath $ToolsDirectory -Force
}

Push-Location $ProjectRoot
try {
    Install-WixToolset
    if (-not $SkipAppBuild) {
        & (Join-Path $PSScriptRoot "build_v1.ps1") -Clean:$Clean
        if ($LASTEXITCODE -ne 0) {
            throw "Application build failed with exit code $LASTEXITCODE"
        }
    }
    $SourceExecutable = Join-Path $PayloadSource "File Indexer V1.exe"
    if (-not (Test-Path -LiteralPath $SourceExecutable -PathType Leaf)) {
        throw "Installed-app payload not found: $SourceExecutable"
    }
    New-Item -ItemType Directory -Force -Path $OutputDirectory, $Intermediate | Out-Null

    # Harvest a frozen copy. The runnable release directory may create transient
    # config/database files during smoke tests, which must never enter the MSI.
    $ExpectedPayload = [IO.Path]::GetFullPath((Join-Path $ProjectRoot "build\installer\payload"))
    if ([IO.Path]::GetFullPath($Payload) -ne $ExpectedPayload) {
        throw "Refusing to replace unexpected installer staging path: $Payload"
    }
    if (Test-Path -LiteralPath $Payload) {
        Remove-Item -LiteralPath $Payload -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $Payload | Out-Null
    Get-ChildItem -LiteralPath $PayloadSource -Force | Copy-Item -Destination $Payload -Recurse -Force
    foreach ($RuntimeName in @("config.yaml", "ui.json", "engine.log", "data")) {
        $RuntimePath = Join-Path $Payload $RuntimeName
        if (Test-Path -LiteralPath $RuntimePath) {
            Remove-Item -LiteralPath $RuntimePath -Recurse -Force
        }
    }
    $Executable = Join-Path $Payload "File Indexer V1.exe"

    $Heat = Join-Path $ToolsDirectory "heat.exe"
    $Candle = Join-Path $ToolsDirectory "candle.exe"
    $Light = Join-Path $ToolsDirectory "light.exe"
    $Harvest = Join-Path $Intermediate "Payload.wxs"
    $PerUserTransform = Join-Path $ProjectRoot "installer\PerUserHarvest.xslt"
    & $Heat dir $Payload -nologo -cg AppFiles -dr INSTALLFOLDER -gg -scom -sreg `
        -sfrag -srd -t $PerUserTransform -var var.PayloadDir -out $Harvest
    if ($LASTEXITCODE -ne 0) { throw "WiX heat failed with exit code $LASTEXITCODE" }

    $Definitions = @(
        "-dPayloadDir=$Payload",
        "-dProjectRoot=$ProjectRoot"
    )
    $ProductObject = Join-Path $Intermediate "Product.wixobj"
    $PayloadObject = Join-Path $Intermediate "Payload.wixobj"
    & $Candle -nologo -arch x64 @Definitions -out $ProductObject "installer\Product.wxs"
    if ($LASTEXITCODE -ne 0) { throw "WiX product compile failed with exit code $LASTEXITCODE" }
    & $Candle -nologo -arch x64 @Definitions -out $PayloadObject $Harvest
    if ($LASTEXITCODE -ne 0) { throw "WiX payload compile failed with exit code $LASTEXITCODE" }

    $CabinetCache = Join-Path $Intermediate "cabcache"
    if (Test-Path -LiteralPath $CabinetCache) {
        Remove-Item -LiteralPath $CabinetCache -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $CabinetCache | Out-Null
    $Msi = Join-Path $OutputDirectory "File Indexer V1 Setup.msi"
    # Leaf directories have explicit RemoveFolder rows from the transform.
    # ICE64 still flags structural parent directories that contain no component
    # of their own; they become empty as their transformed children uninstall.
    & $Light -nologo -cc $CabinetCache -sice:ICE64 -sice:ICE91 `
        -ext WixUIExtension -cultures:en-us -out $Msi $ProductObject $PayloadObject
    if ($LASTEXITCODE -ne 0) { throw "WiX light failed with exit code $LASTEXITCODE" }

    # Run the same internal-consistency checks independently of the linker so
    # future command-line changes cannot accidentally produce an unvalidated
    # package. ICE64 is limited to empty structural parent directories; ICE91
    # is irrelevant for this intentionally per-user installer.
    $Smoke = Join-Path $ToolsDirectory "smoke.exe"
    & $Smoke -nologo -sice:ICE64 -sice:ICE91 $Msi
    if ($LASTEXITCODE -ne 0) { throw "WiX MSI validation failed with exit code $LASTEXITCODE" }
    Write-Host "Built installer: $Msi"
}
finally {
    Pop-Location
}
