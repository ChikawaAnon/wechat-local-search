@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo [ERROR] Python 3.10+ not found & pause & exit /b 1)
python wx_search.py search
pause
