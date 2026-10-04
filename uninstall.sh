#!/bin/sh

set -eu

[ "$(id -u)" -eq 0 ] || {
	echo "Error: run this script as root on OpenWrt." >&2
	exit 1
}

MAX_PATHS="$(uci -q get hitwh_mwan.main.max_paths || true)"
PARENT_DEVICE="$(uci -q get hitwh_mwan.main.parent_device || true)"
PREV_FLOW="$(uci -q get hitwh_mwan.main.previous_flow_offloading || true)"
PREV_FLOW_HW="$(uci -q get hitwh_mwan.main.previous_flow_offloading_hw || true)"
[ -n "$MAX_PATHS" ] || MAX_PATHS=17
[ -n "$PARENT_DEVICE" ] || PARENT_DEVICE=eth1

BACKUP_DIR="/root/hitwh-mwan-backups/uninstall-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
chmod 700 /root/hitwh-mwan-backups "$BACKUP_DIR"
cp /etc/config/network "$BACKUP_DIR/network"
cp /etc/config/firewall "$BACKUP_DIR/firewall"
[ ! -f /etc/config/hitwh_mwan ] || cp /etc/config/hitwh_mwan "$BACKUP_DIR/hitwh_mwan"

/etc/init.d/hitwh-mwan stop >/dev/null 2>&1 || true
/etc/init.d/hitwh-mwan disable >/dev/null 2>&1 || true

WAN_ZONE=""
for Z in $(uci show firewall | sed -n "s/^\(firewall\.[^.]*\)=zone$/\1/p"); do
	[ "$(uci -q get "$Z.name")" = wan ] && WAN_ZONE="$Z"
done

N=2
while [ "$N" -le "$MAX_PATHS" ]; do
	DSEC="wan${N}dev"
	IFACE="wan$N"
	TYPE="$(uci -q get "network.$DSEC.type" || true)"
	PARENT="$(uci -q get "network.$DSEC.ifname" || true)"
	if [ "$TYPE" = macvlan ] && [ "$PARENT" = "$PARENT_DEVICE" ]; then
		ifdown "$IFACE" >/dev/null 2>&1 || true
		uci -q delete "network.$IFACE"
		uci -q delete "network.$DSEC"
		[ -z "$WAN_ZONE" ] || uci -q del_list "$WAN_ZONE.network=$IFACE" || true
	fi
	N=$((N + 1))
done
uci commit network
uci commit firewall

[ -z "$PREV_FLOW" ] || uci set "firewall.@defaults[0].flow_offloading=$PREV_FLOW"
[ -z "$PREV_FLOW_HW" ] || uci set "firewall.@defaults[0].flow_offloading_hw=$PREV_FLOW_HW"
uci commit firewall

nft delete table inet hitwh_mwan >/dev/null 2>&1 || true
rm -f \
	/usr/sbin/hitwh-mwan \
	/usr/sbin/hitwh-mwan-update \
	/usr/sbin/hitwh-mwan-monitor \
	/etc/init.d/hitwh-mwan \
	/etc/hotplug.d/iface/95-hitwh-mwan \
	/usr/share/nftables.d/ruleset-post/90-hitwh-mwan.nft \
	/etc/config/hitwh_mwan

for PATH_TO_REMOVE in \
	/usr/sbin/hitwh-mwan \
	/usr/sbin/hitwh-mwan-update \
	/usr/sbin/hitwh-mwan-monitor \
	/usr/share/nftables.d/ruleset-post/90-hitwh-mwan.nft; do
	sed -i "\\|^$PATH_TO_REMOVE\$|d" /etc/sysupgrade.conf
done

/etc/init.d/network reload
/etc/init.d/firewall reload >/dev/null 2>&1 || true

echo "Uninstalled. Configuration backup: $BACKUP_DIR"
