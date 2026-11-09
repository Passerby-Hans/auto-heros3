# Compile a frozen native checkpoint. This script does not run media or smoke tests.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ExpectedCommit,
      [string]$ObsDir = 'C:\Program Files\obs-studio')
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
. (Join-Path $PSScriptRoot 'build-provenance.ps1')
$before = Get-A3GitState $root
if ($ExpectedCommit -notmatch '^[0-9a-f]{40}$' -or $before.commit -ne $ExpectedCommit -or $before.dirty) {
    throw 'Checkpoint builds require an exact 40-character commit and a clean checkout'
}
& (Join-Path $PSScriptRoot 'build-media-selftest.ps1') -RepoRoot $root -ObsDir $ObsDir -ExpectedCommit $ExpectedCommit -RequireClean
if ($LASTEXITCODE -ne 0) { throw 'Native recorder build failed' }
& (Join-Path $PSScriptRoot 'build-private-outputs.ps1') -ExpectedCommit $ExpectedCommit -RequireClean
if ($LASTEXITCODE -ne 0) { throw 'Private outputs build failed' }
& (Join-Path $root 'platform/sdl-capture-patch/build-private-sdl.ps1') -RepoRoot $root -ExpectedCommit $ExpectedCommit -RequireClean -SkipSmoke
if ($LASTEXITCODE -ne 0) { throw 'Private SDL build failed' }
$summaries = @(
    (Join-Path $root 'platform/obs-recorder/build-media/build_media_summary.json'),
    (Join-Path $root 'platform/obs-recorder/bin/a3-obs-outputs.build.json'),
    (Join-Path $root 'platform/sdl-capture-patch/stage/build.json'))
$records = foreach ($path in $summaries) {
    $summary = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ((Get-FileHash -LiteralPath $summary.provenance -Algorithm SHA256).Hash.ToLowerInvariant() -ne $summary.provenance_sha256) {
        throw "Provenance hash mismatch: $path"
    }
    $record = Get-Content -LiteralPath $summary.provenance -Raw | ConvertFrom-Json
    if ($record.source.commit -ne $ExpectedCommit -or $record.source.dirty -or -not $record.source_unchanged -or $record.state -ne 'built') {
        throw "Invalid frozen build record: $path"
    }
    foreach ($artifact in $record.artifacts) {
        if ((Get-FileHash -LiteralPath $artifact.path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $artifact.sha256) {
            throw "Artifact changed after build: $($artifact.path)"
        }
    }
    [ordered]@{ component = $record.component; provenance = $summary.provenance;
        provenance_sha256 = $summary.provenance_sha256; artifacts = $record.artifacts }
}
$after = Get-A3GitState $root
if (($before | ConvertTo-Json -Compress) -cne ($after | ConvertTo-Json -Compress)) { throw 'Source changed during checkpoint build' }
$manifest = [ordered]@{
    schema = 'a3-native-checkpoint/1'; state = 'built_unvalidated'; source_commit = $ExpectedCommit; source_dirty = $false
    created_at_utc = (Get-Date).ToUniversalTime().ToString('o'); components = @($records)
    validation = 'not_run'; distribution_bundle = 'not_created'
}
$out = Join-Path $root 'platform/obs-recorder/bin'
$id = 'checkpoint-' + [guid]::NewGuid().ToString('N') + '.json'
$path = Join-Path (Join-Path $out 'provenance') $id
Write-A3NewJson $path $manifest
Copy-Item -LiteralPath $path -Destination (Join-Path $out 'native-checkpoint.json') -Force
Write-Host "Checkpoint built, validation not run: $path"
