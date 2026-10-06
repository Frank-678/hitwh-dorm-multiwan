#!/bin/sh

set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

find "$ROOT" -type f \( -name '*.sh' -o -name '*.command' -o -path '*/usr/sbin/*' -o -path '*/usr/libexec/*' -o -path '*/packaging/*' -o -path '*/.githooks/pre-commit' -o -path '*/init.d/*' -o -path '*/hotplug.d/*' \) |
while IFS= read -r FILE; do
	sh -n "$FILE"
done

grep -q "config globals 'main'" "$ROOT/files/etc/config/hitwh_mwan"
grep -q 'destroy table inet hitwh_mwan' "$ROOT/files/usr/share/nftables.d/ruleset-post/90-hitwh-mwan.nft"
grep -q 'hitwh-mwan add' "$ROOT/README.md"

MOCK_DIR="$(mktemp -d)"
trap 'rm -rf "$MOCK_DIR"' EXIT INT TERM
cat >"$MOCK_DIR/uci" <<'EOF'
#!/bin/sh
case "$*" in
	'-q get hitwh_mwan.main.max_paths') echo 16 ;;
	'-q get hitwh_mwan.main.parent_device') echo eth1 ;;
	'-q get hitwh_mwan.main.main_interface') echo wan ;;
	'-q get network.wan6') echo interface ;;
	*) exit 1 ;;
esac
EOF
chmod 755 "$MOCK_DIR/uci"
if HITWH_MWAN_MANAGEMENT_LOCK="$MOCK_DIR/manager.lock" PATH="$MOCK_DIR:$PATH" sh "$ROOT/files/usr/sbin/hitwh-mwan" remove wan6 >"$MOCK_DIR/output" 2>&1; then
	echo "hitwh-mwan remove wan6 unexpectedly succeeded" >&2
	exit 1
fi
grep -q 'was not created by HITwh dorm multi-WAN' "$MOCK_DIR/output"
rm -rf "$MOCK_DIR"
trap - EXIT INT TERM

MOCK_DIR="$(mktemp -d)"
trap 'rm -rf "$MOCK_DIR"' EXIT INT TERM
cat >"$MOCK_DIR/uci" <<'EOF'
#!/bin/sh
case "$*" in
	'-q get hitwh_mwan.main.max_paths') echo 2 ;;
	'-q get hitwh_mwan.main.parent_device') echo eth1 ;;
	'-q get hitwh_mwan.main.main_interface') echo wan ;;
	'-q get network.wan2dev.type') echo macvlan ;;
	'-q get network.wan2dev.ifname') echo eth1 ;;
	'-q get network.wan2dev.macaddr') echo 02:00:00:00:00:02 ;;
	'-q get network.wan2.device') echo macwan2 ;;
	'-q get network.wan2.proto') echo dhcp ;;
	*) exit 1 ;;
esac
EOF
cat >"$MOCK_DIR/update" <<'EOF'
#!/bin/sh
COUNT=0
[ ! -r "$TEST_UPDATE_COUNT" ] || COUNT="$(cat "$TEST_UPDATE_COUNT")"
COUNT=$((COUNT + 1))
echo "$COUNT" >"$TEST_UPDATE_COUNT"
{
	echo 'active_count:2'
	echo 'active_interfaces: wan wan2'
	echo 'wan mac=00:00:00:00:00:01 ip=10.0.0.1 state=active mark=0x101'
	if [ "$COUNT" -eq 1 ]; then
		echo 'wan2 mac=02:00:00:00:00:02 ip=10.0.0.2 state=inactive mark=0x102'
	else
		echo 'wan2 mac=02:00:00:00:00:02 ip=10.0.0.2 state=active mark=0x102'
	fi
} >"$HITWH_MWAN_STATUS"
EOF
cat >"$MOCK_DIR/ifdown" <<'EOF'
#!/bin/sh
echo "ifdown $1" >>"$TEST_ACTIONS"
EOF
cat >"$MOCK_DIR/ifup" <<'EOF'
#!/bin/sh
echo "ifup $1" >>"$TEST_ACTIONS"
EOF
cat >"$MOCK_DIR/ubus" <<'EOF'
#!/bin/sh
echo '{}'
EOF
cat >"$MOCK_DIR/jsonfilter" <<'EOF'
#!/bin/sh
cat >/dev/null
echo 10.0.0.2
EOF
cat >"$MOCK_DIR/sleep" <<'EOF'
#!/bin/sh
[ "$1" != 270 ] || exec /bin/sleep 270
exit 0
EOF
chmod 755 "$MOCK_DIR/uci" "$MOCK_DIR/update" "$MOCK_DIR/ifdown" \
	"$MOCK_DIR/ifup" "$MOCK_DIR/ubus" "$MOCK_DIR/jsonfilter" "$MOCK_DIR/sleep"
TEST_UPDATE_COUNT="$MOCK_DIR/update-count" \
TEST_ACTIONS="$MOCK_DIR/actions" \
HITWH_MWAN_UPDATE="$MOCK_DIR/update" \
HITWH_MWAN_STATUS="$MOCK_DIR/status" \
HITWH_MWAN_REFRESH_LOCK="$MOCK_DIR/refresh.lock" \
PATH="$MOCK_DIR:$PATH" \
	sh "$ROOT/files/usr/sbin/hitwh-mwan" refresh >"$MOCK_DIR/output"
[ ! -f "$MOCK_DIR/actions" ]
grep -q '^RESULT 1 1 0$' "$MOCK_DIR/output"
rm -rf "$MOCK_DIR"
trap - EXIT INT TERM

if command -v shellcheck >/dev/null 2>&1; then
	find "$ROOT" -type f \( -name '*.sh' -o -name '*.command' -o -path '*/usr/sbin/*' -o -path '*/usr/libexec/*' -o -path '*/packaging/*' -o -path '*/.githooks/pre-commit' -o -path '*/init.d/*' -o -path '*/hotplug.d/*' \) -print0 |
		xargs -0 shellcheck --shell=sh --severity=error
fi

echo "All checks passed."
