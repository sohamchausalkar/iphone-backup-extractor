@echo off
cd /d "%~dp0"
python -c "import iphone_backup_decrypt" 2>nul || python -m pip install -r requirements.txt
start "" pythonw app.pyw
