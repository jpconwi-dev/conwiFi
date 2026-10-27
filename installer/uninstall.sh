#!/usr/bin/env bash
# Removes services and firewall rules. Keeps /var/lib/jp-pisowifi (payments!) and /etc/jp-pisowifi unless --purge-data.
set -u
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
PURGE=0; [ "${1:-}" = "--purge-data" ] && PURGE=1
systemctl disable --now jp-pisowifi-update.path jp-pisowifi-ap jp-pisowifi-coin jp-pisowifi-monitor jp-pisowifi jp-pisowifi-network 2>/dev/null
nft delete table inet jp_pisowifi 2>/dev/null
rm -f /etc/systemd/system/jp-pisowifi*.service /etc/systemd/system/jp-pisowifi-update.path
systemctl unmask hostapd 2>/dev/null
rm -f /etc/jp-pisowifi/hostapd.conf /etc/dnsmasq.d/jp-pisowifi.conf /etc/jp-pisowifi/jp-pisowifi.nft /etc/sysctl.d/99-jp-pisowifi.conf
rm -f /etc/NetworkManager/conf.d/99-jp-pisowifi-unmanaged.conf /usr/local/bin/jp-pisowifi
systemctl daemon-reload; systemctl restart dnsmasq 2>/dev/null; nmcli general reload conf 2>/dev/null
rm -rf /opt/jp-pisowifi
if [ $PURGE -eq 1 ]; then rm -rf /var/lib/jp-pisowifi /etc/jp-pisowifi; echo "Data purged."; else echo "Kept /var/lib/jp-pisowifi and /etc/jp-pisowifi (database, backups, config)."; fi
echo "Uninstalled. The LAN interface keeps its address until reboot or 'ip addr flush dev <lan>'."
