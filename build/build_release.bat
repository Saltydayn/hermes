@echo off
REM Builds both the portable zip and the Inno Setup installer in one shot.
REM Just wraps the existing build/build.py flags - see build/build.py's own docstring
REM for details (requires Windows, Inno Setup 6, assets/ffmpeg.exe).

cd /d "%~dp0\.."
python build\build.py --portable --installer
echo.
echo Build finished. Check build\HERMES-*-portable.zip and build\Output\ for the installer.
pause
