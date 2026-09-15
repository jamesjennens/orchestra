@echo off
rem Thin Windows entry point for the kit client.
rem Resolves the kit from this script's own location, so the caller's cwd does not
rem matter; forwards every argument and exits with the client's own code.
rem Set BEADS_PYTHON to one Python executable path (no flags) when "python" is not
rem on PATH. Plain sequential batch only (no labels/goto), so the repository-wide
rem eol=lf convention is safe; no PowerShell and no execution-policy change.
setlocal DisableDelayedExpansion
rem Do not let an inherited variable shadow CMD's dynamic exit status.
set "ERRORLEVEL="
set "KIT=%~dp0"
set "PY=%BEADS_PYTHON%"
if not defined PY set "PY=python"
"%PY%" "%KIT%client.py" %*
exit /b %ERRORLEVEL%
