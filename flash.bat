@echo off
REM Compile and upload the NOVRAM sketch using the arduino-cli bundled with the
REM Arduino IDE.  No IDE window needed.
REM
REM   flash.bat              compile only, then list the ports it can see
REM   flash.bat COMn         compile, then upload to that port
REM
REM ALWAYS UPLOAD WITH THE SOCKET EMPTY.  The bootloader leaves every pin
REM floating for a second or more during an upload; the pull-ups cover that,
REM but there is no reason to have the chip present for it.

set "ACLI=C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
set "SKETCH=%~dp0rw_x2212_uno"

if not exist "%ACLI%" (
  echo Could not find arduino-cli at:
  echo   %ACLI%
  echo Edit the ACLI line in this file to point at your Arduino IDE install.
  exit /b 1
)

echo Compiling...
"%ACLI%" compile --fqbn arduino:avr:uno "%SKETCH%" || exit /b 1

if "%~1"=="" (
  echo.
  echo Compiled. Ports detected:
  "%ACLI%" board list
  echo.
  echo To upload, pass the port name from the list above, e.g.  flash.bat COMn
  exit /b 0
)

echo.
echo Uploading to %~1 ...
"%ACLI%" upload -p %~1 --fqbn arduino:avr:uno "%SKETCH%" || exit /b 1
echo.
echo Done. Verify with:  python novram.py   (it prints the banner on connect)
