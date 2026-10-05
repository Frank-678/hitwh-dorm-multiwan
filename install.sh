#!/bin/sh

set -eu

PROJECT_RAW="${HITWH_MWAN_REPO_RAW:-https://raw.githubusercontent.com/ponder-j/hitwh-dorm-multiwan/main}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd)"
BACKUP_DIR="/root/hitwh-mwan-backups/install-$(date +%Y%m%d-%H%M%S)"
LEGACY_FILES='/usr/sbin/campus-wan
/usr/sbin/campus-mwan-update
/usr/sbin/campus-mwan-monitor
/etc/init.d/campus-mwan
/etc/hotplug.d/iface/95-campus-mwan
/usr/share/nftables.d/ruleset-post/90-campus-mwan.nft
/etc/config/campus_mwan'

FILES='files/usr/sbin/hitwh-mwan
files/usr/sbin/hitwh-mwan-update
files/usr/sbin/hitwh-mwan-monitor
files/etc/init.d/hitwh-mwan
files/etc/init.d/hitwh-mwan-dns
files/etc/hotplug.d/iface/95-hitwh-mwan
files/usr/share/nftables.d/ruleset-post/90-hitwh-mwan.nft
files/etc/config/hitwh_mwan'

die() {
	echo "Error: $*" >&2
	exit 1
}

[ "$(id -u)" -eq 0 ] || die "run this installer as root on OpenWrt"

for CMD in uci ubus jsonfilter nft ip curl fw4; do
	command -v "$CMD" >/dev/null 2>&1 || die "required command is missing: $CMD"
done

modprobe macvlan >/dev/null 2>&1 || true
if [ ! -d /sys/module/macvlan ] && ! opkg status kmod-macvlan 2>/dev/null | grep -q '^Status:.* installed'; then
	die "kmod-macvlan is missing. Install a firmware-matched kmod-macvlan package first."
fi

mkdir -p "$BACKUP_DIR"
chmod 700 /root/hitwh-mwan-backups "$BACKUP_DIR"
cp /etc/config/network "$BACKUP_DIR/network"
cp /etc/config/firewall "$BACKUP_DIR/firewall"
[ ! -f /etc/config/dhcp ] || cp /etc/config/dhcp "$BACKUP_DIR/dhcp"
[ ! -f /etc/config/hitwh_mwan ] || cp /etc/config/hitwh_mwan "$BACKUP_DIR/hitwh_mwan"
echo "$LEGACY_FILES" | while IFS= read -r LEGACY; do
	[ -f "$LEGACY" ] || continue
	DEST="$BACKUP_DIR/legacy$LEGACY"
	mkdir -p "$(dirname "$DEST")"
	cp "$LEGACY" "$DEST"
done

# Migrate an older campus_mwan UCI package before installing the unified names.
if [ ! -f /etc/config/hitwh_mwan ] && [ -f /etc/config/campus_mwan ]; then
	cp /etc/config/campus_mwan /etc/config/hitwh_mwan
fi

download() {
	REL="$1"
	DEST="$2"
	mkdir -p "$(dirname "$DEST")"
	if [ -f "$SCRIPT_DIR/$REL" ]; then
		cp "$SCRIPT_DIR/$REL" "$DEST"
	elif command -v uclient-fetch >/dev/null 2>&1; then
		uclient-fetch -q -O "$DEST" "$PROJECT_RAW/$REL"
	elif command -v wget >/dev/null 2>&1; then
		wget -q -O "$DEST" "$PROJECT_RAW/$REL"
	else
		curl -fsSL "$PROJECT_RAW/$REL" -o "$DEST"
	fi
}

/etc/init.d/hitwh-mwan stop >/dev/null 2>&1 || true
/etc/init.d/campus-mwan stop >/dev/null 2>&1 || true
/etc/init.d/campus-mwan disable >/dev/null 2>&1 || true

echo "$FILES" | while IFS= read -r REL; do
	[ -n "$REL" ] || continue
	DEST="/${REL#files/}"
	if [ "$REL" = files/etc/config/hitwh_mwan ] && [ -f "$DEST" ]; then
		continue
	fi
	download "$REL" "$DEST"
done

chmod 755 \
	/usr/sbin/hitwh-mwan \
	/usr/sbin/hitwh-mwan-update \
	/usr/sbin/hitwh-mwan-monitor \
	/etc/init.d/hitwh-mwan \
	/etc/init.d/hitwh-mwan-dns \
	/etc/hotplug.d/iface/95-hitwh-mwan
chmod 644 \
	/etc/config/hitwh_mwan \
	/usr/share/nftables.d/ruleset-post/90-hitwh-mwan.nft

# Give dnsmasq source-bound DHCP resolvers from every addressed path. Leave an
# existing custom serversfile alone; its resolver setup remains user-managed.
DNS_FILE=/var/run/hitwh-mwan.dns
ROUTER_FAILOVER="$(uci -q get hitwh_mwan.main.router_failover || true)"
[ -n "$ROUTER_FAILOVER" ] || ROUTER_FAILOVER=1
if [ "$ROUTER_FAILOVER" = 1 ] && [ "$(uci -q get 'dhcp.@dnsmasq[0]' || true)" = dnsmasq ]; then
	SERVERS_FILE="$(uci -q get 'dhcp.@dnsmasq[0].serversfile' || true)"
	if [ -z "$SERVERS_FILE" ] || [ "$SERVERS_FILE" = "$DNS_FILE" ]; then
		touch "$DNS_FILE"
		chmod 644 "$DNS_FILE"
		if [ "$SERVERS_FILE" != "$DNS_FILE" ]; then
			uci set "dhcp.@dnsmasq[0].serversfile=$DNS_FILE"
			uci commit dhcp
			/etc/init.d/dnsmasq reload
		fi
		/etc/init.d/hitwh-mwan-dns enable
	else
		echo "Keeping custom dnsmasq serversfile: $SERVERS_FILE. Ensure its DNS works without the main WAN."
	fi
fi

PREV_FLOW="$(uci -q get firewall.@defaults[0].flow_offloading || true)"
PREV_FLOW_HW="$(uci -q get firewall.@defaults[0].flow_offloading_hw || true)"
uci set "hitwh_mwan.main.previous_flow_offloading=${PREV_FLOW:-0}"
uci set "hitwh_mwan.main.previous_flow_offloading_hw=${PREV_FLOW_HW:-0}"
uci commit hitwh_mwan

# Flow offload can bypass connection marks and policy routing on this platform.
uci set firewall.@defaults[0].flow_offloading='0'
uci set firewall.@defaults[0].flow_offloading_hw='0'
uci commit firewall

for PATH_TO_KEEP in \
	/etc/init.d/hitwh-mwan-dns \
	/usr/sbin/hitwh-mwan \
	/usr/sbin/hitwh-mwan-update \
	/usr/sbin/hitwh-mwan-monitor \
	/usr/share/nftables.d/ruleset-post/90-hitwh-mwan.nft; do
	grep -qxF "$PATH_TO_KEEP" /etc/sysupgrade.conf 2>/dev/null || echo "$PATH_TO_KEEP" >>/etc/sysupgrade.conf
done

fw4 check
# Remove legacy hooks before reloading firewall4 so only one marking table runs.
rm -f /etc/hotplug.d/iface/95-campus-mwan \
	/usr/share/nftables.d/ruleset-post/90-campus-mwan.nft
/etc/init.d/firewall reload >/dev/null 2>&1 || true
/etc/init.d/hitwh-mwan enable
/etc/init.d/hitwh-mwan start
sleep 2
/usr/sbin/hitwh-mwan-update || die "the unified hitwh-mwan updater failed"

nft delete table inet campus_mwan >/dev/null 2>&1 || true
rm -f /usr/sbin/campus-wan \
	/usr/sbin/campus-mwan-update \
	/usr/sbin/campus-mwan-monitor \
	/etc/init.d/campus-mwan \
	/etc/config/campus_mwan
for LEGACY in $LEGACY_FILES; do
	sed -i "\|^$LEGACY\$|d" /etc/sysupgrade.conf
done

cat <<EOF
Installed HITwh dorm multi-WAN.
Backups: $BACKUP_DIR

Next steps:
  hitwh-mwan set-main <authenticated-main-MAC>
  hitwh-mwan add <authenticated-second-MAC>
  hitwh-mwan add <authenticated-third-MAC>
  hitwh-mwan list
EOF
