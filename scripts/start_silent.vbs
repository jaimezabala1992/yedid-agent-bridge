' Lanzador silencioso en segundo plano para Yedid Smart Home Hub
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Yedid-Smart-Home"
WshShell.Run "cmd /c ""C:\Yedid-Smart-Home\start_hub.bat""", 0, False
