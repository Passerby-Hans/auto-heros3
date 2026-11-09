# Shared build-time provenance. Dot-source only; this file starts no processes
# other than read-only git queries. Historical records are create-new files.
function Get-A3GitState([string]$Root) {
    $commit = (& git -C $Root rev-parse HEAD 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw "Cannot identify source commit: $Root" }
    $tree = (& git -C $Root rev-parse 'HEAD^{tree}' 2>$null | Out-String).Trim()
    $status = (& git -C $Root status --porcelain --untracked-files=all 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Cannot read source status: $Root" }
    return [ordered]@{ root = $Root; commit = $commit; tree = $tree; dirty = [bool]$status; status = $status }
}

function Get-A3TrackedInputs([string]$Root, [string[]]$Scopes) {
    $paths = @(& git -C $Root ls-files -- @Scopes)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot enumerate tracked build inputs' }
    return @($paths | ForEach-Object { Join-Path $Root $_ })
}

function Get-A3FileRecords([string[]]$Paths) {
    $files = foreach ($path in $Paths) {
        $item = Get-Item -LiteralPath $path -ErrorAction Stop
        if ($item.PSIsContainer) {
            Get-ChildItem -LiteralPath $item.FullName -File -Recurse | Where-Object { $_.FullName -notmatch '[\\/]\.git[\\/]' }
        } else { $item }
    }
    return @($files | Sort-Object FullName -Unique | ForEach-Object {
        [ordered]@{ path = $_.FullName; bytes = $_.Length;
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
    })
}

function Get-A3ToolRecords([string[]]$Paths) {
    return @(Get-A3FileRecords $Paths | ForEach-Object {
        $record = $_
        $record['file_version'] = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($record.path).FileVersion
        $record
    })
}

function Write-A3NewJson([string]$Path, $Value) {
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 16) + "`n")
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read)
    try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
}

function Start-A3BuildProvenance {
    param([string]$Root, [string]$Component, [string]$RecordDir,
          [string[]]$Inputs, [string[]]$VendorRoots = @(), [string[]]$Tools = @(),
          [string]$ExpectedCommit = '', [switch]$RequireClean, $Configuration = @{})
    $source = Get-A3GitState $Root
    if ($ExpectedCommit -and $source.commit -ne $ExpectedCommit) {
        throw "Expected full commit $ExpectedCommit, found $($source.commit)"
    }
    if ($RequireClean -and $source.dirty) { throw 'Clean source required before building' }
    $vendors = @($VendorRoots | ForEach-Object { Get-A3GitState $_ })
    if ($RequireClean -and @($vendors | Where-Object { $_.dirty }).Count) { throw 'Clean vendor source required before building' }
    $id = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssfffffffZ') + '-' + [guid]::NewGuid().ToString('N')
    New-Item -ItemType Directory -Force -Path $RecordDir | Out-Null
    $record = [ordered]@{
        schema = 'a3-build-provenance/1'; build_id = $id; component = $Component; state = 'started'
        started_at_utc = (Get-Date).ToUniversalTime().ToString('o')
        source = $source; vendors = $vendors; inputs = @(Get-A3FileRecords $Inputs)
        tools = @(Get-A3ToolRecords $Tools); configuration = $Configuration
        expected_commit = $ExpectedCommit; require_clean = [bool]$RequireClean
    }
    $startPath = Join-Path $RecordDir ($id + '.start.json')
    Write-A3NewJson $startPath $record
    return @{ Record = $record; Inputs = $Inputs; VendorRoots = $VendorRoots;
        StartPath = $startPath; StartHash = (Get-FileHash -LiteralPath $startPath -Algorithm SHA256).Hash.ToLowerInvariant() }
}

function Complete-A3BuildProvenance {
    param($Context, [string[]]$Artifacts)
    $start = $Context.Record
    $sourceEnd = Get-A3GitState $start.source.root
    $inputsEnd = @(Get-A3FileRecords $Context.Inputs)
    $vendorsEnd = @($Context.VendorRoots | ForEach-Object { Get-A3GitState $_ })
    $unchanged = (($sourceEnd | ConvertTo-Json -Compress) -ceq ($start.source | ConvertTo-Json -Compress)) -and
        (($inputsEnd | ConvertTo-Json -Depth 8 -Compress) -ceq ($start.inputs | ConvertTo-Json -Depth 8 -Compress)) -and
        (($vendorsEnd | ConvertTo-Json -Depth 8 -Compress) -ceq ($start.vendors | ConvertTo-Json -Depth 8 -Compress))
    if ((Get-FileHash -LiteralPath $Context.StartPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Context.StartHash) {
        throw 'Build start record changed; refusing provenance publication'
    }
    $final = [ordered]@{}
    foreach ($key in $start.Keys) { $final[$key] = $start[$key] }
    $final.state = $(if ($unchanged) { 'built' } else { 'source_changed' })
    $final['finished_at_utc'] = (Get-Date).ToUniversalTime().ToString('o')
    $final['start_record'] = $Context.StartPath
    $final['start_record_sha256'] = $Context.StartHash
    $final['source_unchanged'] = $unchanged
    $final['source_end'] = $sourceEnd
    $final['artifacts'] = @(Get-A3FileRecords $Artifacts)
    $finalPath = Join-Path (Split-Path -Parent $Context.StartPath) ($start.build_id + '.json')
    Write-A3NewJson $finalPath $final
    if (-not $unchanged) { throw "Source or inputs changed during build; see $finalPath" }
    return @{ Path = $finalPath; Sha256 = (Get-FileHash -LiteralPath $finalPath -Algorithm SHA256).Hash.ToLowerInvariant(); Record = $final }
}
