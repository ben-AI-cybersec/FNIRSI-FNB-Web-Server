@echo off
cd /d "D:\Uni\IoT_PhD\FNIRSI-FNB-Web-Server"
start "" run.bat
timeout /t 2 /nobreak > nul
start "" "http://localhost:5002"
