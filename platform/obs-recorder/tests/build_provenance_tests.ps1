# Pure filesystem/git tests. No compiler, OBS, SDL or media process is started.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/build-provenance.ps1')
$checks = 0
function Assert-A3([bool]$Condition, [string]$Name) {
    if (-not $Condition) { throw "FAIL $Name" }
    $script:checks++
}
function Assert-A3Throws([scriptblock]$Action, [string]$Pattern, [string]$Name) {
    $caught = ''
    try { & $Action | Out-Null } catch { $caught = $_.Exception.Message }
    Assert-A3 ($caught -match $Pattern) "$Name ($caught)"
}
$taskTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('a3-provenance-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $taskTempRoot | Out-Null
try {
    & git -C $taskTempRoot init -q
    if ($LASTEXITCODE -ne 0) { throw 'git init failed' }
    '/out/' | Set-Content -LiteralPath (Join-Path $taskTempRoot '.gitignore')
    'source' | Set-Content -LiteralPath (Join-Path $taskTempRoot 'input.c')
    & git -C $taskTempRoot add .
    & git -C $taskTempRoot -c user.name=provenance-test -c user.email=test@example.invalid commit -qm fixture
    if ($LASTEXITCODE -ne 0) { throw 'fixture commit failed' }
    $commit = (Get-A3GitState $taskTempRoot).commit
    $out = Join-Path $taskTempRoot 'out'
    New-Item -ItemType Directory -Path $out | Out-Null
    $input = Join-Path $taskTempRoot 'input.c'
    $artifact = Join-Path $out 'fixture.dll'
    'not executable' | Set-Content -LiteralPath $artifact
    $startArgs = @{ Root = $taskTempRoot; Component = 'fixture'; RecordDir = $out; Inputs = @($input);
        ExpectedCommit = $commit; RequireClean = $true; Configuration = @{ flag = 'test' } }
    $ctx = Start-A3BuildProvenance @startArgs
    $done = Complete-A3BuildProvenance $ctx @($artifact)
    Assert-A3 ($done.Record.source.commit -eq $commit -and -not $done.Record.source.dirty) 'build-time identity'
    Assert-A3 ($done.Record.source_unchanged -and $done.Record.state -eq 'built') 'unchanged build passes'
    Assert-A3 ($done.Record.artifacts[0].sha256 -eq (Get-FileHash $artifact).Hash.ToLowerInvariant()) 'artifact hash bound'
    Assert-A3 ($done.Record.inputs[0].sha256 -eq (Get-FileHash $input).Hash.ToLowerInvariant()) 'input hash bound'
    Assert-A3 (Test-Path -LiteralPath $ctx.StartPath) 'start record preserved'
    Assert-A3Throws { Complete-A3BuildProvenance $ctx @($artifact) } 'exists|存在' 'historical final cannot be overwritten'
    $shortArgs = $startArgs.Clone(); $shortArgs.ExpectedCommit = $commit.Substring(0, 7)
    Assert-A3Throws { Start-A3BuildProvenance @shortArgs } 'Expected full commit' 'short identity rejected'
    'dirty' | Set-Content -LiteralPath $input
    Assert-A3Throws { Start-A3BuildProvenance @startArgs } 'Clean source' 'dirty source rejected'
    & git -C $taskTempRoot checkout -- input.c
    $ctx2 = Start-A3BuildProvenance @startArgs
    'mutated during build' | Set-Content -LiteralPath $input
    Assert-A3Throws { Complete-A3BuildProvenance $ctx2 @($artifact) } 'changed during build' 'mid-build source edit rejected'
    & git -C $taskTempRoot checkout -- input.c
    $ignoredInput = Join-Path $out 'vendor.h'
    'vendor original' | Set-Content -LiteralPath $ignoredInput
    $vendorArgs = $startArgs.Clone(); $vendorArgs.Inputs = @($input, $ignoredInput)
    $ctx3 = Start-A3BuildProvenance @vendorArgs
    'vendor changed' | Set-Content -LiteralPath $ignoredInput
    Assert-A3Throws { Complete-A3BuildProvenance $ctx3 @($artifact) } 'changed during build' 'ignored dependency edit rejected'
    $ctx4 = Start-A3BuildProvenance @startArgs
    'tampered' | Add-Content -LiteralPath $ctx4.StartPath
    Assert-A3Throws { Complete-A3BuildProvenance $ctx4 @($artifact) } 'start record changed' 'start tamper rejected'
    $ctx5 = Start-A3BuildProvenance @startArgs
    & git -C $taskTempRoot -c user.name=provenance-test -c user.email=test@example.invalid commit --allow-empty -qm moved
    Assert-A3Throws { Complete-A3BuildProvenance $ctx5 @($artifact) } 'changed during build' 'post-build commit cannot relabel provenance'
    Write-Host "ALL_PASS checks=$checks failures=0"
} finally {
    $resolved = [System.IO.Path]::GetFullPath($taskTempRoot)
    $tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if ($resolved.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolved) -like 'a3-provenance-*') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    } else { throw 'Refusing cleanup outside task temp directory' }
}
