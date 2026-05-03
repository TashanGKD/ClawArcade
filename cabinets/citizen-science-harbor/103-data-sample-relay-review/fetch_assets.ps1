$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$gpAssetUrl = if ($env:RELAY_GP_ASSET_URL) { $env:RELAY_GP_ASSET_URL } else { "http://49.233.162.81:8788/all_sample_gp.tar" }
$scatterAssetUrl = if ($env:RELAY_SCATTER_ASSET_URL) { $env:RELAY_SCATTER_ASSET_URL } else { "http://49.233.162.81:8788/all_sample_scatter.tar" }

function Ensure-Asset($name, $assetUrl) {
  $imageDir = Join-Path $root $name
  $tarPath = Join-Path $root "$name.tar"

  if ((Test-Path $imageDir) -and ((Get-ChildItem $imageDir -Filter *.png -File -ErrorAction SilentlyContinue | Select-Object -First 1) -ne $null)) {
    Write-Host "$name already exists; skip download."
    return
  }

  Write-Host "Downloading $name assets..."
  Write-Host $assetUrl
  Invoke-WebRequest -Uri $assetUrl -OutFile $tarPath

  Write-Host "Extracting $name.tar..."
  tar -xf $tarPath -C $root

  $count = (Get-ChildItem $imageDir -Filter *.png -File -ErrorAction SilentlyContinue | Measure-Object).Count
  if ($count -le 0) {
    throw "No PNG files found in $name after extraction."
  }
  Write-Host "Ready: $count $name images."
}

Ensure-Asset "all_sample_gp" $gpAssetUrl
Ensure-Asset "all_sample_scatter" $scatterAssetUrl
