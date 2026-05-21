@echo off
setlocal

set "ROOT=%~dp0"

echo Starting Job Applier...
echo Backend  -> http://localhost:8001
echo Frontend -> http://localhost:5173
echo.

:: Backend: run from job_applier\ so "backend.*" imports resolve
start "Job Applier - Backend" /D "%ROOT%" cmd /k python run_backend.py

:: Frontend: serve the pre-built dist as a SPA (npx serve handles 404->index.html)
start "Job Applier - Frontend" cmd /k npx serve -s "%ROOT%frontend\dist" -l 5173 --no-clipboard

echo Done. Two windows opened.
pause
