' bilibili-to-doc launcher (used by the desktop shortcut)
' Opens a console window showing the running status.
' Closing that console window stops the program.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\common\bili2doc"
sh.Run """C:\Python314\python.exe"" ""C:\common\bili2doc\app.py""", 1, False
