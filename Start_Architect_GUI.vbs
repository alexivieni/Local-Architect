Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
Here = FSO.GetParentFolderName(WScript.ScriptFullName)
BatPath = Here & "\Start_Architect_GUI.bat"
WshShell.Run Chr(34) & BatPath & Chr(34), 0, False
Set WshShell = Nothing
Set FSO = Nothing
