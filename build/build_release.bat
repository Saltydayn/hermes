@echo off
REM Builds the Inno Setup installer. Just wraps the existing build/build.py flags -
REM see build/build.py's own docstring for details (requires Windows and Inno Setup 6;
REM ffmpeg is not bundled, the app downloads it on demand).

cd /d "%~dp0\.."
python build\build.py --installer
echo.
echo Build finished. Check build\Output\ for the installer.
pause
