param(
    [Parameter(Mandatory = $true)]
    [string]$Project,
    [string]$SkillsRoot = (Join-Path $env:USERPROFILE ".codex\skills"),
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$Runner = Join-Path $SkillsRoot "ai-production-orchestrator\scripts\orchestrator.py"
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "The orchestrator skill is missing. Run windows/setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $Project -PathType Container)) {
    throw "Project directory is missing. Initialize a production project first."
}
if (-not (Get-Command $Python -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Pass -Python with the chosen Python executable."
}
# The executable and arguments are separate; never use Invoke-Expression.
& $Python $Runner run $Project --skills-root $SkillsRoot
$RunExit = $LASTEXITCODE
switch ($RunExit) {
    0 {
        Write-Host "Local steps completed. Read the returned status: technical completion and final acceptance are different states."
    }
    10 {
        Write-Host "A persisted action queue needs the host agent."
        Write-Host "Ask Codex to continue this project and consume actions.json for creation, provider polling or real review."
        Write-Host "This PowerShell wrapper does not call host MCP tools or provide unattended cloud generation."
    }
    20 {
        Write-Warning "The project is blocked or a stage failed. Inspect state.json and the returned diagnostics, then resume."
    }
    default {
        Write-Warning "The runner failed before it could advance. Inspect its error output; no stage was automatically approved."
    }
}
exit $RunExit
