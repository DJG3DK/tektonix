#!/usr/bin/env pwsh
# The Windows installer's decisions, tested without Docker or Windows.
#
# The script itself cannot be run here -- it starts containers -- so the parts
# that can silently corrupt an operator's configuration are separated out and
# checked directly. Run: pwsh tests/test_install_ps1.ps1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..' 'install.ps1')

$script:failed = 0
$script:ran = 0
function It {
    param([string]$Name, [scriptblock]$Body)
    $script:ran++
    try { & $Body; Write-Host "  ok   $Name" }
    catch { $script:failed++; Write-Host "  FAIL $Name" -ForegroundColor Red; Write-Host "       $($_.Exception.Message)" }
}
function Expect-Equal {
    param($Actual, $Expected, [string]$What = 'value')
    if ($Actual -ne $Expected) { throw "$What was [$Actual], expected [$Expected]" }
}
function Expect-True { param($Condition, [string]$What) if (-not $Condition) { throw "expected $What" } }

Write-Host 'Set-EnvLine'

It 'replaces a key in place instead of adding a second assignment' {
    # Two assignments of one key is the failure that looks like the value
    # being ignored: which one wins depends on the parser.
    $out = Set-EnvLine -Content "A=1`nPROJECTS_DIR=/old`nB=2`n" -Key 'PROJECTS_DIR' -Value '/new'
    Expect-Equal $out "A=1`nPROJECTS_DIR=/new`nB=2`n"
    Expect-Equal ([regex]::Matches($out, '(?m)^PROJECTS_DIR=').Count) 1 'assignment count'
}

It 'appends a key that is not there yet' {
    Expect-Equal (Set-EnvLine -Content "A=1`n" -Key 'B' -Value '2') "A=1`nB=2`n"
}

It 'adds a newline before appending to content that lacks one' {
    Expect-Equal (Set-EnvLine -Content 'A=1' -Key 'B' -Value '2') "A=1`nB=2`n"
}

It 'writes into empty content' {
    Expect-Equal (Set-EnvLine -Content '' -Key 'A' -Value '1') "A=1`n"
}

It 'leaves a commented-out line as documentation and adds the real assignment' {
    $out = Set-EnvLine -Content "# PROJECTS_DIR=C:\Users\you\code`n" -Key 'PROJECTS_DIR' -Value 'C:\code'
    Expect-True ($out -match '(?m)^# PROJECTS_DIR=') 'the comment to survive'
    Expect-True ($out -match '(?m)^PROJECTS_DIR=C:\\code$') 'the real assignment to be added'
}

It 'does not rewrite a different key that merely starts the same' {
    $out = Set-EnvLine -Content "PROJECTS_DIR_OLD=/keep`n" -Key 'PROJECTS_DIR' -Value '/new'
    Expect-True ($out -match '(?m)^PROJECTS_DIR_OLD=/keep$') 'the other key to be untouched'
    Expect-True ($out -match '(?m)^PROJECTS_DIR=/new$') 'the new key to be added'
}

It 'keeps a value containing = intact, because keys do' {
    $out = Set-EnvLine -Content "OPENROUTER_API_KEY=old`n" -Key 'OPENROUTER_API_KEY' -Value 'sk-or-v1-a=b=c'
    Expect-True ($out -match '(?m)^OPENROUTER_API_KEY=sk-or-v1-a=b=c$') 'the whole value to survive'
}

It 'does not let a $ in a value be read as a regex replacement group' {
    # [regex]::Replace treats $1 in a replacement STRING as a capture
    # reference; a key containing one would be silently mangled.
    $out = Set-EnvLine -Content "K=old`n" -Key 'K' -Value 'a$1b'
    Expect-True ($out -match '(?m)^K=a\$1b$') 'the literal $1 to survive'
}

Write-Host 'Format-ProjectsDir'

It 'strips the quotes a person pastes with a path' {
    Expect-Equal (Format-ProjectsDir '"C:\Users\you\code"') 'C:\Users\you\code'
}

It 'trims a trailing separator that would double up when joined' {
    Expect-Equal (Format-ProjectsDir 'C:\Users\you\code\') 'C:\Users\you\code'
    Expect-Equal (Format-ProjectsDir '/home/you/code/') '/home/you/code'
}

It 'leaves a drive root alone rather than turning it into a drive letter' {
    Expect-Equal (Format-ProjectsDir 'C:\') 'C:\'
}

Write-Host 'Test-ProjectsDirValue'

It 'accepts a Windows path' { Expect-True (Test-ProjectsDirValue -Value 'C:\Users\you\code').Ok 'accepted' }
It 'accepts a Unix path'    { Expect-True (Test-ProjectsDirValue -Value '/home/you/code').Ok 'accepted' }
It 'accepts a UNC path'     { Expect-True (Test-ProjectsDirValue -Value '\\nas\code').Ok 'accepted' }

It 'rejects a relative path, which the Docker daemon cannot resolve' {
    $v = Test-ProjectsDirValue -Value 'code'
    Expect-True (-not $v.Ok) 'rejection'
    Expect-True ($v.Reason -match 'absolute') 'a reason naming the problem'
}

It 'rejects an empty value' {
    Expect-True (-not (Test-ProjectsDirValue -Value '  ').Ok) 'rejection'
}

Write-Host 'Read-EnvValues'

It 'reads assignments and ignores comments and blank lines' {
    $v = Read-EnvValues -Content "# a comment`n`nA=1`n  B = 2  `n"
    Expect-Equal $v['A'] '1'
    Expect-Equal $v['B'] '2'
    Expect-Equal $v.Count 2 'key count'
}

It 'keeps everything after the first = in a value' {
    $v = Read-EnvValues -Content "K=a=b=c`n"
    Expect-Equal $v['K'] 'a=b=c'
}

It 'ignores a line with no =' {
    Expect-Equal (Read-EnvValues -Content "nonsense`n").Count 0 'key count'
}

Write-Host ''
if ($script:failed -gt 0) { Write-Host "$script:failed of $script:ran failed" -ForegroundColor Red; exit 1 }
Write-Host "$script:ran passed" -ForegroundColor Green
