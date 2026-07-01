@echo off
REM ozbot - build gamex86.dll (32-bit) with MSVC.
REM The Quake II engine is 32-bit, so this MUST produce an x86 DLL.
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM --- locate MSVC x86 toolchain if cl is not already on PATH ---
where cl >nul 2>nul
if %errorlevel%==0 goto havecl

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" set "VSWHERE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
  echo [ozbot] vswhere.exe not found; install Visual Studio with the "Desktop development with C++" workload.
  exit /b 1
)
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -property installationPath`) do set "VSINSTALL=%%i"
if not defined VSINSTALL (
  echo [ozbot] No Visual Studio installation found.
  exit /b 1
)
set "VCVARS=%VSINSTALL%\VC\Auxiliary\Build\vcvarsall.bat"
if not exist "%VCVARS%" (
  echo [ozbot] vcvarsall.bat not found at "%VCVARS%".
  exit /b 1
)
echo [ozbot] Initializing MSVC x86 environment...
call "%VCVARS%" x86
if errorlevel 1 (
  echo [ozbot] Failed to initialize the MSVC x86 environment.
  exit /b 1
)

:havecl
if not exist dist mkdir dist
if not exist build mkdir build
del /q build\*.obj >nul 2>nul

echo [ozbot] Compiling...
cl /nologo /c /MT /W3 /EHsc /O2 /Fobuild\ ^
   /D WIN32 /D NDEBUG /D _WINDOWS /D _CRT_SECURE_NO_WARNINGS /D C_ONLY ^
   src\*.c
if errorlevel 1 (
  echo [ozbot] Compile failed.
  exit /b 1
)

echo [ozbot] Linking...
link /nologo /base:0x20000000 /subsystem:windows /dll /machine:I386 ^
   /def:src\game.def /out:dist\gamex86.dll build\*.obj ^
   kernel32.lib user32.lib winmm.lib
if errorlevel 1 (
  echo [ozbot] Link failed.
  exit /b 1
)

echo [ozbot] Built dist\gamex86.dll
endlocal
