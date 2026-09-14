@echo off
chcp 65001 >nul
cd /d "%~dp0"
python clip.py
if errorlevel 1 pause
