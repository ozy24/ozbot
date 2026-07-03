@echo off
REM ozbot - record YOUR OWN inputs (+ a demo) on q2dm1 for jump analysis.
REM
REM Launches a listen server you play in, with:
REM   - bot_inputlog 1  -> per-frame usercmd trace to ozbot\logs\q2dm1_<timestamp>.jsonl
REM                        (forward/side/up move, jump, attack, view yaw/pitch, speed)
REM   - a synchronized .dm2 demo to ozbot\demos\inputs_*.dm2 (for replay / cross-check)
REM
REM Use it:  run this, do your box double-jump + strafe jump to the Megahealth a
REM          few times, then type  quit  in the console (~) or use the menu to exit.
REM          (Quitting cleanly is what flushes the .dm2 -- don't just close the window.)
REM Then analyze with:  py tools\input_view.py <newest ozbot\logs\q2dm1_*.jsonl>
REM
REM Override the install location with:  set Q2DIR=C:\path\to\quake2
setlocal
if "%Q2DIR%"=="" set "Q2DIR=%~dp0..\engine"
cd /d "%Q2DIR%"

if not exist "ozbot\gamex86.dll" (
  echo [ozbot] ozbot\gamex86.dll not found under %Q2DIR%. Run build.bat + deploy.bat first.
  exit /b 1
)

set "DEMO=inputs_%RANDOM%"

REM Generate a config so the quoting for cl_beginmapcmd (starts the demo on map
REM load) is reliable -- command-line quoting through the batch is not.
> "ozbot\record_inputs.cfg" (
  echo set deathmatch 1
  echo set maxclients 16
  echo set bot_count 0
  echo set bot_inputlog 1
  echo set vid_fullscreen 0
  echo set vid_geometry "1280x720"
  echo set cl_beginmapcmd "record %DEMO%"
  echo map q2dm1
)

echo.
echo [ozbot] Input log : ozbot\logs\q2dm1_^<timestamp^>.jsonl   (bot_inputlog 1)
echo [ozbot] Demo       : ozbot\demos\%DEMO%.dm2
echo [ozbot] Play q2dm1, do your box-hops + strafe jump, then type  quit  (or use the menu).
echo.

q2pro.exe +set game ozbot +exec record_inputs.cfg
endlocal
