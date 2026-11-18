#!/bin/sh
cd "$(dirname "$0")" && python3 munch_installer.py "$@"
echo; printf "Press Enter to close..."; read _
