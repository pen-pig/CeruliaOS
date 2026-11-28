@echo off
setlocal
cd /d "%~dp0"

rem ===== build-only mode: build only, no QEMU, no pause =====
set "BUILD_ONLY=0"
if /i "%~1"=="build-only" set "BUILD_ONLY=1"

rem ===== locate python =====
set "PYEXE="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%P"
rem WindowsApps may contain a Store alias rather than a working interpreter.
if defined PYEXE (
    "%PYEXE%" -c "import sys" >nul 2>&1
    if errorlevel 1 set "PYEXE="
)
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "D:\Python315\python.exe" set "PYEXE=D:\Python315\python.exe"
if not defined PYEXE (
    echo [ERROR] Python interpreter not found. Install Python or add it to PATH.
    exit /b 1
)
"%PYEXE%" -c "import sys" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python interpreter could not start: "%PYEXE%"
    exit /b 1
)
rem Ninja rules also invoke python; use the same validated interpreter there.
for %%P in ("%PYEXE%") do set "PATH=%%~dpP;%PATH%"

rem Catch incomplete checkouts before Ninja reports a generated file missing.
"%PYEXE%" tools/check_build_inputs.py
if errorlevel 1 exit /b 1

rem ===== generate build_auto.ninja =====
rem All toolchain paths are resolved by tools\gen_ninja.py via ezos_env.py, which
rem derives them from the project location. Nothing here is hardcoded to a drive
rem letter, so a copy of the repo on another disk builds without editing files.
rem The rewrite MUST stay byte-level: PowerShell Get-Content decodes UTF-8 as GBK
rem and an orphan lead byte (e.g. 0x82 left from U+3002) swallows the following
rem 0x0A, merging two ninja lines into "unexpected indent".
"%PYEXE%" "%~dp0tools\gen_ninja.py" --src "%~dp0build.ninja" --out "%~dp0build_auto.ninja"
if errorlevel 1 (
    echo [ERROR] failed to generate build_auto.ninja
    echo         see the message above for the missing tool or path
    pause
    exit /b 1
)

rem ===== ninja resolution: prefer ninja.exe next to this script, fallback to PATH =====
rem Python's PATH changes do not propagate back to this batch process.
for /f "delims=" %%L in ('""%PYEXE%" tools\gen_ninja.py --linker-dir"') do (
    set "PATH=%%L;%PATH%"
    for %%V in ("%%L\..\..\..\..\..\..\..\Common7\Tools\VsDevCmd.bat") do set "EZOS_VSDEV=%%~fV"
)
rem Host build scripts also link against SDK import libraries (kernel32.lib).
if not defined VSCMD_VER if defined EZOS_VSDEV if exist "%EZOS_VSDEV%" call "%EZOS_VSDEV%" -arch=x64 -no_logo
set "NINJA=%~dp0ninja.exe"
if not exist "%NINJA%" (
    where ninja >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] ninja not found. Add ninja to PATH or place it next to this script.
        pause
        exit /b 1
    )
    set "NINJA=ninja"
)

rem ===== parallel build -j = CPU logical cores =====
set "NINJA_JOBS=%NUMBER_OF_PROCESSORS%"
if /i "%~1"=="vmware" goto vmware
if "%BUILD_ONLY%"=="1" (
    echo [build-only] ninja -j%NINJA_JOBS% building os-image.bin, no QEMU...
    "%NINJA%" -f "%~dp0build_auto.ninja" -j%NINJA_JOBS% os-image.bin
    if errorlevel 1 (
        echo [ERROR] build failed.
        exit /b 1
    )
    echo [build-only] build done: os-image.bin
    exit /b 0
)
echo [parallel] ninja -j%NINJA_JOBS% building and launching QEMU...
"%NINJA%" -f "%~dp0build_auto.ninja" -j%NINJA_JOBS% run
set "RUN_RC=%ERRORLEVEL%"
pause
exit /b %RUN_RC%

:vmware
"%NINJA%" -f "%~dp0build_auto.ninja" -j%NINJA_JOBS% vmware
exit /b %ERRORLEVEL%
