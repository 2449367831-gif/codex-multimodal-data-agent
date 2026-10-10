"""Validate restart targeting without stopping real processes or tasks."""

import json
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/restart_hidden.ps1"


@pytest.mark.skipif(os.name != "nt", reason="Windows restart targeting")
def test_restart_matches_only_exact_installation_runtime_commands():
    program = r'''
$ErrorActionPreference = 'Stop'
$projectRoot = 'C:\department agent'
$scriptPath = '__SCRIPT__'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Invalid restart script' }
$function = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-AgentRuntimeKind' }, $true)
if (-not $function) { throw 'Missing runtime matcher' }
. ([scriptblock]::Create($function.Extent.Text))
$python = '"C:\department agent\.venv\Scripts\pythonw.exe"'
$launch = '"C:\department agent\scripts\launch_hidden.py" --supervise --no-browser'
$chat = '-m uvicorn app.chat_api:app --host 127.0.0.1 --port 8021'
$backend = '-m uvicorn app.query_backend:app --host 127.0.0.1 --port 8010'
$cases = @(
    @('pythonw.exe', "$python $launch"),
    @('python.exe', "$python $chat"),
    @('pythonw.exe', "$python $backend"),
    @('powershell.exe', "$python $chat"),
    @('pythonw.exe', '"C:\another agent\.venv\Scripts\pythonw.exe" ' + $chat),
    @('pythonw.exe', "$python -m pytest --mention app.chat_api:app"),
    @('pythonw.exe', "$python $chat --workers 4"),
    @('pythonw.exe', "$python -c 'app.chat_api:app'"),
    @('pythonw.exe', "$python $launch --test"),
    @('pythonw.exe', ($python + ' "C:\another agent\scripts\launch_hidden.py" --supervise --no-browser')),
    @('pythonw.exe', ($python + ' ' + $chat.Replace('127.0.0.1', '0.0.0.0'))),
    @('pythonw.exe', $null)
)
$results = @($cases | ForEach-Object {
    $kind = Get-AgentRuntimeKind ([pscustomobject]@{ Name = $_[0]; CommandLine = $_[1] })
    if ($kind) { $kind } else { 'unmanaged' }
})
ConvertTo-Json -Compress -InputObject $results
'''.replace("__SCRIPT__", str(SCRIPT).replace("'", "''"))
    # A no-console parent can have an invalid inherited stdin HANDLE. Supply
    # all standard streams explicitly, including stdin, for hidden execution.
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", program],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW, check=True,
    )
    assert json.loads(result.stdout) == ["supervisor", "service", "service"] + ["unmanaged"] * 9
