#!/bin/sh
# Offline entry point; package hooks own setup and LuCI registration.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root on OpenWrt.' >&2; exit 1; }
command -v opkg >/dev/null || { echo 'OpenWrt 24.10/opkg is required.' >&2; exit 1; }
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PACKAGE="${1:-}"
if [ -z "$PACKAGE" ]; then
    for CANDIDATE in "$SCRIPT_DIR"/dist/luci-app-hitwh-mwan_*_all.ipk; do
        [ ! -f "$CANDIDATE" ] || PACKAGE="$CANDIDATE"
    done
fi
[ -n "$PACKAGE" ] && [ -f "$PACKAGE" ] || {
    echo 'Transfer the built IPK over SSH, then run: sh install.sh /tmp/luci-app-hitwh-mwan_VERSION_all.ipk' >&2
    exit 1
}
exec opkg install "$PACKAGE"
