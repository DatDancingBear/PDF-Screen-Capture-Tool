@echo off
REM Forward ALL dropped files to Python, not just the first one.
REM %* = every argument passed to this .bat, fully preserved including spaces/quotes.
REM Make sure to change the path according to where the python script is

python "C:\PATH\TO\pdf_capture_tool_unified.py" %*
