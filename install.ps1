# Verinoda installer for Windows (PowerShell 5.1 or later).
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/ozcinax-star/verinoda/main/install.ps1 | iex"
#
# What it does, in order (read it before running it - that is good practice for any piped script):
#   1. uses `uv` if it is on PATH, otherwise installs uv with its official installer (https://astral.sh/uv);
#   2. installs Verinoda as an isolated uv tool: no git and no pre-installed Python needed
#      (uv downloads a suitable Python if none is found). Files are copied, not hardlinked, so
#      sandboxed agents such as Codex can import the package (docs/AGENT-VERIFICATION.md);
#   3. makes sure the tool directory is on PATH (`uv tool update-shell`);
#   4. prints the next step: `verinoda setup` inside a project.
# Running it again upgrades to the latest code on the chosen ref.
#
# Options (environment variables, because `irm | iex` cannot pass arguments):
#   VERINODA_REF              branch, tag or commit to install (default: main)
#   VERINODA_EXTRAS           extras to install (default: precise; set to "none" for none)
#   VERINODA_SPEC             full requirement to install instead (advanced / testing)
#   VERINODA_NO_MODIFY_PATH   set to 1 to leave PATH alone
#   VERINODA_NO_UV_INSTALL    set to 1 to fail instead of installing uv

# Exit codes of native commands are checked explicitly; 'Stop' would turn uv's progress output on
# stderr into terminating errors under Windows PowerShell 5.1.
$ErrorActionPreference = 'Continue'

function Write-Step([string]$msg) { Write-Host "verinoda-install: $msg" }

function Install-Verinoda {
    $repoUrl = 'https://github.com/ozcinax-star/verinoda'
    $ref = if ($env:VERINODA_REF) { $env:VERINODA_REF } else { 'main' }
    $extras = if ($null -ne $env:VERINODA_EXTRAS) { $env:VERINODA_EXTRAS } else { 'precise' }

    # 1. uv
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        if ($env:VERINODA_NO_UV_INSTALL -eq '1') {
            Write-Step 'uv is not installed and VERINODA_NO_UV_INSTALL=1; install uv first: https://docs.astral.sh/uv/'
            return 1
        }
        Write-Step 'uv not found - installing it with the official installer (https://astral.sh/uv)'
        powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex' | Out-Host
        if ($LASTEXITCODE -ne 0) { Write-Step 'uv installation failed'; return 1 }
        $candidates = @("$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")
        foreach ($c in $candidates) { if (Test-Path "$c\uv.exe") { $env:Path = "$c;$env:Path" } }
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if (-not $uv) { Write-Step 'uv was installed but is not on PATH yet; open a new terminal and run this again'; return 1 }
    }
    Write-Step ("using " + (& uv --version))

    # 2. Verinoda
    if ($env:VERINODA_SPEC) {
        $spec = $env:VERINODA_SPEC
    } else {
        $source = "$repoUrl/archive/$ref.zip"
        if ($extras -and $extras -ne 'none') { $spec = "verinoda[$extras] @ $source" } else { $spec = "verinoda @ $source" }
    }
    Write-Step "installing $spec"
    & uv tool install --force --reinstall-package verinoda --link-mode copy $spec | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Step 'uv tool install failed (see the output above)'; return 1 }

    # 3. PATH
    if ($env:VERINODA_NO_MODIFY_PATH -ne '1') {
        & uv tool update-shell | Out-Null
    }
    $binDir = (& uv tool dir --bin).Trim()
    if (($env:Path -split ';') -notcontains $binDir) { $env:Path = "$binDir;$env:Path" }
    $exe = Join-Path $binDir 'verinoda.exe'
    if (-not (Test-Path $exe)) { Write-Step "verinoda.exe not found in $binDir"; return 1 }
    Write-Step ("installed " + (& $exe --version))

    # 4. next steps
    Write-Host ''
    Write-Host 'Next, inside a project folder:'
    Write-Host '    verinoda setup          # index the code and connect Claude Code / Codex if they are installed'
    Write-Host ''
    if ($env:VERINODA_NO_MODIFY_PATH -eq '1') {
        Write-Host "PATH was not changed (VERINODA_NO_MODIFY_PATH=1); the program is $exe"
    } else {
        Write-Host 'If `verinoda` is not found, open a new terminal (PATH was updated for new shells).'
    }
    return 0
}

$code = Install-Verinoda
if ($code -ne 0) { Write-Step "failed (exit $code)" }
