#!/bin/sh
# Double-click to open the GUI. Pass --cli to use the command-line version instead.
cd "$(dirname "$0")"
if [ "$1" = "--cli" ]; then shift; python3 woa_installer.py "$@"; echo; printf "Press Enter to close..."; read _
else python3 woa_gui.py; fi
