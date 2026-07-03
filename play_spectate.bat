@echo off
REM ozbot - launch a local listen server and join as a SPECTATOR (chase cam).
REM Bots fight on q2dm1; you watch from the sidelines. Console (~):
REM   Fire = enter/exit chase cam    Jump = next target    Prev weapon = prev target
REM   Use  = toggle eyecam / third-person while chasing
REM   bot_debug 1  = draw nav paths and combat debug beams
REM Override the install location with:  set Q2DIR=C:\path\to\quake2
setlocal
if "%Q2DIR%"=="" set "Q2DIR=%~dp0..\engine"
cd /d "%Q2DIR%"

if not exist "ozbot\gamex86.dll" (
  echo [ozbot] ozbot\gamex86.dll not found under %Q2DIR%. Run build.bat + deploy.bat first.
  exit /b 1
)

q2pro.exe +set game ozbot +set deathmatch 1 +set maxclients 16 ^
  +set bot_count 4 +set bot_skill 0.6 +set spectator 1 +map q2dm1
endlocal
