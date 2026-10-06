#!/bin/bash
# Double-click this file on macOS. All paths are relative to this file.
cd "$(dirname "$0")" || exit 1

for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
        "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        "$candidate" start.py "$@"
        result=$?
        if [ "$result" -ne 0 ]; then
            echo
            read -r -p "Press Enter to close this window..."
        fi
        exit "$result"
    fi
done

echo "Python 3.12 or newer is required."
echo "Install it from https://www.python.org/downloads/ and double-click Start.command again."
read -r -p "Press Enter to close this window..."
exit 1
