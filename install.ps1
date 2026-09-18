#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Tektonix installer for Windows and macOS: the Docker bundle, set up for you.

.DESCRIPTION
    install.sh is the HOST install -- nginx, certbot, pm2, a package manager --
    and none of that exists on Windows. The bundle is the path that already
    runs anywhere Docker does, and the agent's own bind-mount translation was
    written for Windows drive letters on purpose (agent/tools/sandbox.py,
    host_path). What was missing was never the runtime. It was this: the four
    steps between a fresh copy of the source and a running console, one of
    which is `cp`, which is not a command Windows has.

    So this does what docker/README.md tells a person to do by hand. It checks
    Docker is actually usable, writes the .env, asks for the two values that
    have no sensible default, and brings the stack up.

    Nothing here is Windows-only -- it runs under PowerShell 7 on macOS and
    Linux too -- but Windows is the reason it exists.

.PARAMETER ProjectsDir
    Where your repositories live on this machine. Everything under it appears
    to the agent as /projects/<name>.

.PARAMETER OpenRouterKey
    Your OpenRouter API key. Read from the OPENROUTER_API_KEY environment
    variable when not passed.

.PARAMETER DryRun
    Print every action without performing any of it. Same idea as
    `./install.sh --dry-run`.

.PARAMETER Yes
    Never prompt. Every value must already be present, in the environment or
    as a parameter, or the run fails saying which one is missing.

.EXAMPLE
    .\install.ps1
    Asks for what it needs and starts the stack.

.EXAMPLE
    .\install.ps1 -DryRun
    Shows what it would do, touching nothing.
#>
[CmdletBinding()]
param(
    [string]$ProjectsDir,
    [string]$OpenRouterKey,
    [switch]$DryRun,
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# --- output -----------------------------------------------------------------

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "    $Message" }
function Write-Warn { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Yellow }
function Write-Fail { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Red }

# --- the pure decisions -----------------------------------------------------
# Separated from the doing so they can be tested without Docker, a repository
# or a Windows machine -- see tests/test_install_ps1.ps1.

<#
Set one KEY=value in a .env file's text, returning the new text.

Rewrites the key in place when it is already there, so re-running never leaves
two assignments of the same name -- the later one silently wins in some
parsers and the earlier one in others, and the operator sees their value
apparently ignored. A commented-out line is left alone and the real assignment
added, because a comment is documentation, not configuration.
#>
function Set-EnvLine {
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Content,
        [Parameter(Mandatory)][string]$Key,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Value
    )
    $pattern = '(?m)^\s*' + [regex]::Escape($Key) + '\s*=.*$'
    $line = "$Key=$Value"
    if ($Content -match $pattern) {
        return [regex]::Replace($Content, $pattern, { $line })
    }
    if ($Content.Length -gt 0 -and -not $Content.EndsWith("`n")) { $Content += "`n" }
    return $Content + $line + "`n"
}

<#
Normalise a path the operator typed for use as PROJECTS_DIR.

Strips the quotes a person pastes along with a path, and trims the trailing
separator: compose joins onto this value, and "C:\code\" would produce
"C:\code\\name".
#>
function Format-ProjectsDir {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Raw)
    $v = $Raw.Trim().Trim('"').Trim("'").Trim()
    if ($v.Length -gt 3) { $v = $v.TrimEnd('\', '/') }
    return $v
}

<#
Whether a value is usable as PROJECTS_DIR, and why not when it is not.

Relative paths are rejected rather than resolved: the value is handed to the
Docker daemon, which resolves bind mounts against its own notion of the
filesystem and not against the directory this script happened to run in.
#>
function Test-ProjectsDirValue {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return @{ Ok = $false; Reason = 'no value given' }
    }
    $isWindowsAbsolute = $Value -match '^[A-Za-z]:[\\/]'
    $isUnixAbsolute = $Value.StartsWith('/')
    $isUnc = $Value.StartsWith('\\')
    if (-not ($isWindowsAbsolute -or $isUnixAbsolute -or $isUnc)) {
        return @{ Ok = $false; Reason = "must be an absolute path (got '$Value')" }
    }
    return @{ Ok = $true; Reason = '' }
}

<#
Read KEY=value pairs out of .env text, ignoring comments and blank lines.
Used to decide what still has to be asked for on a re-run.
#>
function Read-EnvValues {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Content)
    $out = @{}
    foreach ($raw in ($Content -split "`r?`n")) {
        $line = $raw.Trim()
        if ($line.Length -eq 0 -or $line.StartsWith('#')) { continue }
        $i = $line.IndexOf('=')
        if ($i -lt 1) { continue }
        $out[$line.Substring(0, $i).Trim()] = $line.Substring($i + 1).Trim()
    }
    return $out
}

# --- the doing --------------------------------------------------------------

function Invoke-Step {
    param([Parameter(Mandatory)][string]$Describe, [Parameter(Mandatory)][scriptblock]$Action)
    if ($DryRun) { Write-Note "would $Describe"; return $null }
    return & $Action
}

function Test-DockerUsable {
    # `docker info` rather than `docker --version`: Desktop can be installed
    # and not running, which is the common Windows case and gives a completely
    # different error further along if it is not caught here.
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Fail 'Docker is not installed.'
        Write-Note 'Install Docker Desktop: https://docs.docker.com/get-started/get-docker/'
        return $false
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'Docker is installed but not running.'
        Write-Note 'Start Docker Desktop and wait for it to report Running, then run this again.'
        return $false
    }
    docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail 'This Docker has no `compose` subcommand (v2 is required).'
        Write-Note 'Docker Desktop ships it; on Linux install the docker-compose-plugin package.'
        return $false
    }
    return $true
}

function Request-Value {
    param(
        [Parameter(Mandatory)][string]$Prompt,
        [string]$Current,
        [switch]$Secret
    )
    if (-not [string]::IsNullOrWhiteSpace($Current)) { return $Current }
    if ($Yes) { throw "$Prompt is required, and -Yes forbids asking for it." }
    if ($Secret) {
        $secure = Read-Host -Prompt $Prompt -AsSecureString
        return [System.Net.NetworkCredential]::new('', $secure).Password
    }
    return (Read-Host -Prompt $Prompt)
}

function Main {
    $root = Split-Path -Parent $PSCommandPath
    Set-Location $root

    Write-Step 'Checking this is a Tektonix checkout'
    foreach ($needed in @('docker-compose.yml', 'docker/.env.example')) {
        if (-not (Test-Path (Join-Path $root $needed))) {
            Write-Fail "$needed is missing -- run this from the root of the source tree."
            exit 1
        }
    }
    Write-Note "found the bundle in $root"

    Write-Step 'Checking Docker'
    if (-not $DryRun) {
        if (-not (Test-DockerUsable)) { exit 1 }
        Write-Note 'Docker is running and has compose v2'
    } else {
        Write-Note 'would check that Docker is installed, running, and has compose v2'
    }

    Write-Step 'Writing .env'
    $envPath = Join-Path $root '.env'
    $content = if (Test-Path $envPath) {
        Write-Note '.env exists -- keeping every value already in it'
        Get-Content $envPath -Raw
    } else {
        Get-Content (Join-Path $root 'docker/.env.example') -Raw
    }

    $existing = Read-EnvValues -Content $content

    $key = $OpenRouterKey
    if ([string]::IsNullOrWhiteSpace($key)) { $key = $env:OPENROUTER_API_KEY }
    if ([string]::IsNullOrWhiteSpace($key) -and $existing.ContainsKey('OPENROUTER_API_KEY')) {
        $key = $existing['OPENROUTER_API_KEY']
    }
    $key = Request-Value -Prompt 'OpenRouter API key' -Current $key -Secret

    $dir = $ProjectsDir
    if ([string]::IsNullOrWhiteSpace($dir) -and $existing.ContainsKey('PROJECTS_DIR')) {
        $dir = $existing['PROJECTS_DIR']
    }
    $dir = Format-ProjectsDir (Request-Value -Prompt 'Folder holding your repositories' -Current $dir)

    $verdict = Test-ProjectsDirValue -Value $dir
    if (-not $verdict.Ok) {
        Write-Fail "PROJECTS_DIR: $($verdict.Reason)"
        Write-Note 'Example on Windows:  C:\Users\you\code'
        exit 1
    }
    if (-not $DryRun -and -not (Test-Path $dir)) {
        # Not fatal: compose would create it, but as an empty directory the
        # agent then reports as having no repositories in it, which reads as a
        # bug rather than as a typo.
        Write-Warn "$dir does not exist yet. Check the spelling if that is a surprise."
    }

    $content = Set-EnvLine -Content $content -Key 'OPENROUTER_API_KEY' -Value $key
    $content = Set-EnvLine -Content $content -Key 'PROJECTS_DIR' -Value $dir
    Invoke-Step "write $envPath" { Set-Content -Path $envPath -Value $content -NoNewline -Encoding utf8 }
    if (-not $DryRun) { Write-Note "wrote $envPath" }

    Write-Step 'Starting the stack'
    Invoke-Step 'run: docker compose up -d --build' {
        docker compose up -d --build
        if ($LASTEXITCODE -ne 0) { Write-Fail 'docker compose failed -- see the output above.'; exit 1 }
    }

    Write-Step 'Done'
    if ($DryRun) {
        Write-Note 'Nothing was changed. Run without -DryRun to do it for real.'
        return
    }
    Write-Note 'Console:  http://localhost:8100'
    Write-Note 'The first-run admin password is printed once, in the agent log:'
    Write-Note '  docker compose logs agent'
    Write-Note 'Re-running this script is safe, and is also how you upgrade.'
}

# Dot-sourcing (`. ./install.ps1`) loads the functions WITHOUT installing
# anything, which is what the test file does.
if ($MyInvocation.InvocationName -ne '.') { Main }
