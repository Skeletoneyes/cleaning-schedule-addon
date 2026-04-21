@echo off
REM Weekly reconciliation pull. Invoked by Windows Task Scheduler.
REM Runs the pull script; output directory goes under .secrets/pulls/.
cd /d "%~dp0\.."
python scripts\reconcile_pull.py >> .secrets\pulls\weekly.log 2>&1
