@echo off
rem Start the pipeline GUI (orchestrator) with the Python of this repository's .venv.
rem
rem   start_gui.bat "D:\MyProject"              start on that project folder (an empty folder becomes a new project)
rem   start_gui.bat "D:\MyProject" --port 8090  options go after the folder (--port, --no-browser, --host 0.0.0.0)
rem   drag a project folder onto this file      same as the first line
rem   double-click                              asks for the project folder
rem
rem Closing this window stops the GUI; running jobs go on in their own process.

setlocal
set "REPO=%~dp0"
set "PYTHON=%REPO%.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo No Python environment found: %PYTHON%
    echo Create it first, in %REPO%:  python -m venv .venv  and  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

set "PROJECT=%~1"
if "%PROJECT%"=="" (
    set /p "PROJECT=Project folder: "
)
if "%PROJECT%"=="" (
    echo No project folder given.
    pause
    exit /b 1
)
rem Strip quotes typed or pasted at the prompt (e.g. from Explorer's "Copy as path")
set "PROJECT=%PROJECT:"=%"

rem Options after the folder are passed on
set "OPTIONS="
shift
:options
if "%~1"=="" goto run
set "OPTIONS=%OPTIONS% %1"
shift
goto options

:run
cd /d "%REPO%"
"%PYTHON%" -m orchestrator "%PROJECT%" %OPTIONS%
if errorlevel 1 pause
