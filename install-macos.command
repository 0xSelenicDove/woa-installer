#!/bin/sh
cd "$(dirname "$0")" && python3 woa_installer.py "$@"
echo; printf "Press Enter to close..."; read _
