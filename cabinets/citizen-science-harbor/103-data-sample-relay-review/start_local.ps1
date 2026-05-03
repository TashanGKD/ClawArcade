$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = if ($env:PORT) { $env:PORT } else { "8788" }
if (-not $env:RELAY_PUBLIC_BASE_URL) {
  $env:RELAY_PUBLIC_BASE_URL = "http://127.0.0.1:$port"
}

Set-Location $root

function Test-ImageAsset($name) {
  $path = Join-Path $root $name
  return (Test-Path $path) -and ((Get-ChildItem $path -Filter *.png -File -ErrorAction SilentlyContinue | Select-Object -First 1) -ne $null)
}

if (-not (Test-ImageAsset "all_sample_gp") -or -not (Test-ImageAsset "all_sample_scatter")) {
  & "$root\fetch_assets.ps1"
}
python .\relay_server.py --host 127.0.0.1 --port $port
