Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\Uni\IoT_PhD\FNIRSI-FNB-Web-Server"
WshShell.Run "run.bat", 0, False
WScript.Sleep 2000
WshShell.Run "http://localhost:5002"
