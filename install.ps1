# comfyui-weaver installer.
# Creates a private venv (never touches ComfyUI's own Python), installs the
# MCP server deps, writes .mcp.json for Claude Code, and self-tests.
# Idempotent — safe to re-run.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-DataDir <ComfyUI data dir>]

param(
    [string]$DataDir = ""
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"

Write-Host "comfyui-weaver installer"
Write-Host "Package root: $root"

# --- locate the ComfyUI data directory ------------------------------------
if (-not $DataDir) {
    $parent = Split-Path $root -Parent
    if ((Test-Path (Join-Path $parent "models")) -and (Test-Path (Join-Path $parent "output"))) {
        $DataDir = $parent   # cloned inside the data dir (recommended layout)
    } else {
        Write-Error "Cannot auto-detect the ComfyUI data dir (models/ + output/ not found in parent). Re-run with -DataDir <path>."
    }
}
Write-Host "ComfyUI data dir: $DataDir"

# --- find a base Python (3.10+) and create the venv ------------------------
$versionProbe = "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if (-not (Test-Path $venvPython)) {
    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        & py -3 -c $versionProbe
        if ($LASTEXITCODE -eq 0) { Write-Host "Creating venv (py -3)..."; & py -3 -m venv $venv }
    }
    if (-not (Test-Path $venvPython) -and (Get-Command "python" -ErrorAction SilentlyContinue)) {
        & python -c $versionProbe
        if ($LASTEXITCODE -eq 0) { Write-Host "Creating venv (python)..."; & python -m venv $venv }
    }
    if (-not (Test-Path $venvPython)) {
        Write-Error "No Python 3.10+ found on PATH (or venv creation failed)."
    }
} else {
    Write-Host "venv already exists - reusing."
}

Write-Host "Installing dependencies..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $root "requirements.txt") --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed." }

# --- write .mcp.json into the data dir -------------------------------------
$mcpPath = Join-Path $DataDir ".mcp.json"
$serverScript = Join-Path $root "server\comfy_mcp_server.py"
$entry = @{
    command = $venvPython
    args = @($serverScript)
    env = @{ COMFY_DATA_DIR = $DataDir; COMFY_CLOUD_API_KEY = "" }
}
$config = @{ mcpServers = @{ comfyui = $entry } }
if (Test-Path $mcpPath) {
    try {
        $existing = Get-Content $mcpPath -Raw | ConvertFrom-Json
        if ($existing.mcpServers) {
            $existing.mcpServers | Add-Member -NotePropertyName "comfyui" -NotePropertyValue $entry -Force
            $config = $existing
        }
    } catch { Write-Host "(existing .mcp.json unreadable - overwriting)" }
}
$config | ConvertTo-Json -Depth 6 | Set-Content $mcpPath -Encoding utf8
Write-Host "Wrote $mcpPath"

Write-Host "Running offline self-test..."
& $venvPython (Join-Path $root "tests\smoke_test.py") --offline
if ($LASTEXITCODE -ne 0) { Write-Error "Self-test failed - see output above." }

Write-Host ""
Write-Host "Done. Restart Claude Code in $DataDir and approve the 'comfyui' server."
Write-Host "Optional: copy docs\skills\* to $DataDir\.claude\skills\ so Claude"
Write-Host "knows the workflow format and etiquette out of the box."
