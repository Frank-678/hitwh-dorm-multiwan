#!/bin/sh
cd "$(dirname "$0")" || exit 1
exec python3 -X utf8 server.py "$@"
