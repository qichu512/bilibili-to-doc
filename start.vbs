' bilibili-to-doc launcher (used by the desktop shortcut)
' Opens start.bat's console window; closing that window stops the program.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run """" & dir & "\start.bat""", 1, False
