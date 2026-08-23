@echo off
REM ============================================================================
REM  backup_ha_exports.bat
REM  Copies the Home Assistant config-export folder from the Samba share to
REM  this PC.
REM
REM  Run it by hand to test, then schedule it (see SETUP_pc_backup_copy.md).
REM
REM  UNC paths are used deliberately. A mapped drive letter such as Z: only
REM  exists inside your interactive login session, so a scheduled task running
REM  "whether user is logged on or not" would not see it.
REM ============================================================================

setlocal EnableDelayedExpansion

REM ---- settings --------------------------------------------------------------
set "SERVER=\\homeassistant\share"
set "DEST=D:\Backups\HomeAssistant"
set "KEEPLOGS=30"

REM ADDITIVE (default): never deletes from the PC copy, so generations pruned
REM on the server are retained here — this PC becomes the long-term archive.
set "MODE=/E"

REM EXACT MIRROR: uncomment to make the PC an identical copy, which DOES delete
REM anything the server has pruned. Only use if you want a true mirror.
REM set "MODE=/MIR"
REM ----------------------------------------------------------------------------

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmm"') do set "STAMP=%%i"
set "LOGDIR=%DEST%\_logs"
set "LOG=%LOGDIR%\ha_export_copy_%STAMP%.log"

if not exist "%DEST%"   mkdir "%DEST%"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo ============================================================ >>"%LOG%"
echo  Home Assistant export copy - %DATE% %TIME%                  >>"%LOG%"
echo  Source: %SERVER%   Mode: %MODE%                             >>"%LOG%"
echo ============================================================ >>"%LOG%"

REM Reachability check first, so a dead network gives one clear error rather
REM than two pages of robocopy retries.
if not exist "%SERVER%\" (
    echo ERROR: cannot reach %SERVER% >>"%LOG%"
    echo ERROR: cannot reach %SERVER%
    exit /b 99
)

set "FAILED=0"

echo. >>"%LOG%"
robocopy "%SERVER%\ha_config_backup" "%DEST%\config_export" %MODE% ^
    /COPY:DAT /DCOPY:DAT /R:2 /W:5 /NP /NDL /NFL /TEE /LOG+:"%LOG%"
if errorlevel 8 set "FAILED=1"

REM ---- tidy old logs ---------------------------------------------------------
forfiles /P "%LOGDIR%" /M ha_export_copy_*.log /D -%KEEPLOGS% /C "cmd /c del @path" 2>nul

echo. >>"%LOG%"
if "%FAILED%"=="1" (
    echo RESULT: FAILED - see entries above >>"%LOG%"
    echo RESULT: FAILED - see "%LOG%"
    exit /b 1
) else (
    echo RESULT: OK >>"%LOG%"
    echo RESULT: OK - log at "%LOG%"
    exit /b 0
)
