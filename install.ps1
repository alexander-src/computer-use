$ErrorActionPreference = "Stop"

$RepoDir = $PSScriptRoot
$PluginName = "computer-use"
$TargetDir = if ($env:ANTIGRAVITY_PLUGIN_DIR) { $env:ANTIGRAVITY_PLUGIN_DIR } else { Join-Path $env:USERPROFILE ".gemini\config\plugins\$PluginName" }

# Probe real Python 3.10+ executables (skips the 0-byte Windows Store python.exe alias)
$PythonExe = $null
$Candidates = @(
    @("py", "-3"),
    @("python3"),
    @("python")
)
foreach ($cand in $Candidates) {
    $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    $extra = if ($cand.Count -gt 1) { $cand[1..($cand.Count - 1)] } else { @() }
    try {
        $probe = & $cmd.Source @extra -c "import sys; assert sys.version_info >= (3, 10); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $probe) {
            $PythonExe = ($probe | Select-Object -Last 1).Trim()
            if ($PythonExe -and (Test-Path $PythonExe)) { break }
        }
    } catch {}
}

if (-not $PythonExe) {
    Write-Error "Python 3.10+ not found. Install Python from https://www.python.org/downloads/ (and disable the Windows Store python.exe alias if enabled)."
    exit 1
}

Write-Host "Using Python: $PythonExe"
Write-Host "Installing Antigravity plugin '$PluginName' -> $TargetDir"

# Ensure numpy is available (omit --user when inside a virtualenv/conda env)
$HasNumpy = $false
try {
    & $PythonExe -c "import numpy" 2>$null
    if ($LASTEXITCODE -eq 0) { $HasNumpy = $true }
} catch {}

if (-not $HasNumpy) {
    Write-Host "Installing numpy (only Python dependency required on Windows)..."
    $InVenv = (& $PythonExe -c "import sys; print('1' if sys.prefix != getattr(sys, 'base_prefix', sys.prefix) else '0')").Trim()
    if ($InVenv -eq "1") {
        & $PythonExe -m pip install --quiet numpy
    } else {
        & $PythonExe -m pip install --user --quiet numpy
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install numpy (exit code $LASTEXITCODE)."
        exit $LASTEXITCODE
    }
}

$Dirs = @(
    $TargetDir,
    (Join-Path $TargetDir "assets"),
    (Join-Path $TargetDir "agents"),
    (Join-Path $TargetDir "rules"),
    (Join-Path $TargetDir "scripts"),
    (Join-Path $TargetDir "skills\computer-use")
)
foreach ($d in $Dirs) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

$RepoFull = (Resolve-Path $RepoDir).Path.TrimEnd("\")
$TargetFull = (Resolve-Path $TargetDir).Path.TrimEnd("\")
if ($RepoFull -ine $TargetFull) {
    Copy-Item (Join-Path $RepoDir "plugin.json") (Join-Path $TargetDir "plugin.json") -Force
    Copy-Item (Join-Path $RepoDir "README.md") (Join-Path $TargetDir "README.md") -Force
    Copy-Item (Join-Path $RepoDir "assets\logo.svg") (Join-Path $TargetDir "assets\logo.svg") -Force
    Copy-Item (Join-Path $RepoDir "agents\computer-use.md") (Join-Path $TargetDir "agents\computer-use.md") -Force
    Copy-Item (Join-Path $RepoDir "rules\AGENTS.md") (Join-Path $TargetDir "rules\AGENTS.md") -Force
    Copy-Item (Join-Path $RepoDir "skills\computer-use\SKILL.md") (Join-Path $TargetDir "skills\computer-use\SKILL.md") -Force
    Copy-Item (Join-Path $RepoDir "scripts\*.py") (Join-Path $TargetDir "scripts\") -Force
}

$ServerScript = Join-Path $TargetDir "scripts\computer_use_mcp.py"
$McpConfig = @{
    mcpServers = @{
        "computer-use" = @{
            command = $PythonExe
            args = [string[]]@($ServerScript)
            env = @{ OMP_THREAD_LIMIT = "1" }
        }
    }
}
$McpJson = ConvertTo-Json -InputObject $McpConfig -Depth 5
[System.IO.File]::WriteAllText((Join-Path $TargetDir "mcp_config.json"), $McpJson + "`n", (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "Running non-destructive Windows selftest..."
& $PythonExe $ServerScript --selftest
if ($LASTEXITCODE -ne 0) {
    Write-Error "Selftest failed with exit code $LASTEXITCODE."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Done. Restart Antigravity to load the newly installed '$PluginName' plugin."
