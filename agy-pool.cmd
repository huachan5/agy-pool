@echo off
py -3.12 -I -c "import sys; sys.path.insert(0, sys.argv.pop(1)); from agy_pool.cli import main; sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8'); sys.exit(main())" "%~dp0." %*
exit /b %ERRORLEVEL%
