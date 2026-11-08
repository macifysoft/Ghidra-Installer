@echo off
setlocal EnableExtensions
title Ghidra Installer Builder

REM ==========================================================
REM CONFIG
REM ==========================================================
set "SCRIPT=ghidra_installer_windows.py"
set "APP_NAME=GhidraInstaller"

set "PY_VERSION=3.14.0"
set "PY_SHORT=314"

echo ===================================================
echo        Ghidra Installer EXE Builder
echo ===================================================
echo.

cd /d "%~dp0"

REM ==========================================================
REM Verify script exists
REM ==========================================================
if not exist "%SCRIPT%" (
    echo [ERROR] Cannot find:
    echo     %SCRIPT%
    echo.
    pause
    exit /b 1
)

echo [INFO] Python script found:
echo        %SCRIPT%
echo.

REM ==========================================================
REM Detect Python
REM ==========================================================
echo [INFO] Checking Python...

set "PYTHON_CMD="

py --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    goto PY_FOUND
)

python --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto PY_FOUND
)

REM ==========================================================
REM Install Python (only once)
REM ==========================================================
echo [INFO] Python not detected.
echo [INFO] Downloading Python %PY_VERSION%...
echo.

set "PY_URL=https://www.python.org/ftp/python/%PY_VERSION%/python-%PY_VERSION%-amd64.exe"
set "PY_INSTALLER=%TEMP%\python-installer.exe"

if exist "%PY_INSTALLER%" (
    del /f /q "%PY_INSTALLER%" >nul 2>&1
)

echo [INFO] Downloading installer...

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest '%PY_URL%' -OutFile '%PY_INSTALLER%' -UseBasicParsing } catch { $wc = New-Object System.Net.WebClient; $wc.DownloadFile('%PY_URL%','%PY_INSTALLER%') }"

if not exist "%PY_INSTALLER%" (
    echo.
    echo [ERROR] Failed to download Python.
    echo.
    echo Install manually:
    echo https://www.python.org/downloads/windows/
    echo.
    pause
    exit /b 1
)

echo [INFO] Python downloaded.
echo.
REM Derive major.minor from full version (e.g. 3.14.0 -> 3.14)
for /f "tokens=1,2 delims=." %%A in ("%PY_VERSION%") do set "PY_MAJOR_MINOR=%%A.%%B"

REM Clean up stale state from previous failed installs - this is what causes error 1603
echo [INFO] Clearing any previous Python %PY_MAJOR_MINOR% install state...
if exist "%LocalAppData%\Programs\Python\Python%PY_SHORT%" (
    echo [INFO] Removing leftover install directory...
    rmdir /s /q "%LocalAppData%\Programs\Python\Python%PY_SHORT%" 2>nul
)
reg delete "HKCU\Software\Python\PythonCore\%PY_MAJOR_MINOR%" /f >nul 2>&1
reg delete "HKLM\Software\Python\PythonCore\%PY_MAJOR_MINOR%" /f >nul 2>&1
echo [INFO] Done.
echo.

REM Unblock the file so Windows SmartScreen does not silently kill it
powershell -NoProfile -Command "Unblock-File -Path '%PY_INSTALLER%'" 2>nul

echo [INFO] Installing Python %PY_VERSION%...
echo [INFO] This may take 1-2 minutes. Please wait...
echo.

"%PY_INSTALLER%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_pip=1 Include_test=0

echo [INFO] Installer finished (exit code: %errorlevel%).

REM Prepend so Python takes priority over the Windows Store redirect stub
set "PATH=%LocalAppData%\Programs\Python\Python%PY_SHORT%;%LocalAppData%\Programs\Python\Python%PY_SHORT%\Scripts;%PATH%"

REM Detect installed Python once
py --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    goto PY_FOUND
)

python --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto PY_FOUND
)

echo.
echo [ERROR] Python installation failed.
pause
exit /b 1

:PY_FOUND

echo [INFO] Python ready:
%PYTHON_CMD% --version
echo.

REM ==========================================================
REM Ensure pip
REM ==========================================================
echo [INFO] Updating pip...

%PYTHON_CMD% -m ensurepip --upgrade >nul 2>&1
%PYTHON_CMD% -m pip install --upgrade pip

if errorlevel 1 (
    echo [WARNING] pip update failed.
)

echo.

REM ==========================================================
REM Install PyInstaller
REM ==========================================================
echo [INFO] Checking PyInstaller...

%PYTHON_CMD% -m PyInstaller --version >nul 2>&1

if errorlevel 1 (
    echo [INFO] Installing PyInstaller...
    echo.

    %PYTHON_CMD% -m pip install pyinstaller

    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

echo [INFO] PyInstaller ready:
%PYTHON_CMD% -m PyInstaller --version
echo.

REM ==========================================================
REM Clean previous builds
REM ==========================================================
echo [INFO] Cleaning previous build files...

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

del /q *.spec >nul 2>&1

echo Done.
echo.

REM ==========================================================
REM Build EXE
REM ==========================================================
echo ===================================================
echo BUILD STARTED
echo ===================================================
echo.
echo This may take several minutes...
echo.

REM Download Ghidra icon
set "ICO_URL=https://raw.githubusercontent.com/NationalSecurityAgency/ghidra/master/Ghidra/RuntimeScripts/Windows/support/ghidra.ico"
set "ICO_FILE=ghidra_icon.ico"
powershell -NoProfile -NonInteractive -Command "try { Invoke-WebRequest -Uri '%ICO_URL%' -OutFile '%ICO_FILE%' -UseBasicParsing } catch {}"

set "ICON_OPT="
if exist "%ICO_FILE%" set "ICON_OPT=--icon %ICO_FILE%"

%PYTHON_CMD% -m PyInstaller ^
--onefile ^
--windowed ^
--clean ^
--name "%APP_NAME%" ^
%ICON_OPT% ^
--hidden-import "tkinter" ^
--hidden-import "tkinter.filedialog" ^
--hidden-import "tkinter.messagebox" ^
--hidden-import "tkinter.ttk" ^
--hidden-import "tkinter.font" ^
--hidden-import "tkinter.scrolledtext" ^
--hidden-import "winreg" ^
--hidden-import "ssl" ^
--hidden-import "urllib.request" ^
--hidden-import "urllib.error" ^
--hidden-import "urllib.parse" ^
--hidden-import "http.client" ^
--hidden-import "email.message" ^
--hidden-import "email.utils" ^
--hidden-import "traceback" ^
--hidden-import "ctypes" ^
--hidden-import "zipfile" ^
--hidden-import "tempfile" ^
--hidden-import "platform" ^
--collect-all "certifi" ^
--collect-all "tkinter" ^
"%SCRIPT%"

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo ===================================================
echo BUILD COMPLETE
echo ===================================================
echo.

if exist "dist\%APP_NAME%.exe" (
    echo EXE created:
    echo.
    echo %CD%\dist\%APP_NAME%.exe
    echo.

    start "" "%CD%\dist"
) else (
    echo [WARNING] Build completed but EXE not found.
)

echo.
pause