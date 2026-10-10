Option Explicit

Dim fso, shell, scriptsRoot, projectRoot, pythonwPath, pythonPath, launcherPath, interpreter
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptsRoot = fso.GetParentFolderName(WScript.ScriptFullName)
projectRoot = fso.GetParentFolderName(scriptsRoot)
pythonwPath = projectRoot & "\.venv\Scripts\pythonw.exe"
pythonPath = projectRoot & "\.venv\Scripts\python.exe"
launcherPath = scriptsRoot & "\launch_hidden.py"

If fso.FileExists(pythonwPath) Then
    interpreter = pythonwPath
Else
    interpreter = pythonPath
End If

If fso.FileExists(interpreter) And fso.FileExists(launcherPath) Then
    shell.CurrentDirectory = projectRoot
    Dim commandLine
    commandLine = Quote(interpreter) & " " & Quote(launcherPath)
    If WScript.Arguments.Count > 0 Then
        commandLine = commandLine & " " & WScript.Arguments(0)
    End If
    shell.Run commandLine, 0, False
End If

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
