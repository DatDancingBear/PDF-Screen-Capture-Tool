@echo off
REM Forward ALL dropped files to Python, not just the first one.
REM %* = every argument passed to this .bat, fully preserved including spaces/quotes.

python PDF_Screen_Capture_Tool-Unified-Single-Batch-newgx.py %*