#!/usr/bin/env bash
# Emergency: remove JP-PisoWiFi firewall rules, then rebuild kernel state from the database.
echo "Removing JP-PisoWiFi firewall table..."; jp-pisowifi firewall-restore
read -r -p "Re-apply network and sessions now? [y/N] " a
if [[ "$a" =~ ^[Yy] ]]; then jp-pisowifi network-up && systemctl restart jp-pisowifi-monitor; fi
