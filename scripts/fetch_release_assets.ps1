# Fetch FreeBo's big binaries (kept OUT of git) from a GitHub Release into .\release-staging\.
# Configure: $env:FREEBO_RELEASE_REPO = "owner/freebo"; $env:FREEBO_RELEASE_TAG = "latest" (default).
# Uses the `gh` CLI if available (works with private repos), else falls back to public asset URLs.
$ErrorActionPreference = "Stop"
$repo = $env:FREEBO_RELEASE_REPO
$tag  = if ($env:FREEBO_RELEASE_TAG) { $env:FREEBO_RELEASE_TAG } else { "latest" }
$dest = "release-staging"
$assets = @(
  "freebo-cred-collector-rola.apk",
  "freebo-cred-collector-ebohome.apk",
  "freebo-wheelhouse-windows-amd64.tar.gz"
)
if (-not $repo) { Write-Error "Set `$env:FREEBO_RELEASE_REPO = 'owner/repo' (and optionally FREEBO_RELEASE_TAG)."; exit 2 }
New-Item -ItemType Directory -Force -Path $dest | Out-Null

if (Get-Command gh -ErrorAction SilentlyContinue) {
  Write-Host "[fetch] gh release download ($tag) from $repo -> $dest/"
  $tagArg = if ($tag -eq "latest") { @() } else { @($tag) }
  & gh release download @tagArg --repo $repo --dir $dest --clobber
} else {
  Write-Host "[fetch] gh not found; trying public asset URLs"
  $base = if ($tag -eq "latest") { "https://github.com/$repo/releases/latest/download" } else { "https://github.com/$repo/releases/download/$tag" }
  foreach ($a in $assets) {
    try { Invoke-WebRequest -UseBasicParsing "$base/$a" -OutFile (Join-Path $dest $a); Write-Host "  + $a" }
    catch { Write-Host "  - skip $a (not present)" }
  }
}
Write-Host "[fetch] done -> $dest/"
Get-ChildItem $dest -ErrorAction SilentlyContinue | Select-Object Name,@{n='MB';e={[math]::Round($_.Length/1MB,1)}} | Format-Table -AutoSize
