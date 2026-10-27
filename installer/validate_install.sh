#!/usr/bin/env bash
set -u
CONF=/etc/jp-pisowifi/config.json
PY=/opt/jp-pisowifi/venv/bin/python
export JP_PISOWIFI_CONFIG=$CONF
cd /opt/jp-pisowifi/current 2>/dev/null || { echo "not installed"; exit 1; }
rc=0
echo "-- application health"
$PY -m backend.cli health || rc=1
echo "-- portal HTTP"
curl -fsS --max-time 5 http://127.0.0.1/health && echo || { echo "portal not answering on port 80"; rc=1; }
echo "-- firewall table"
nft list table inet jp_pisowifi >/dev/null 2>&1 && echo "jp_pisowifi table loaded" || { echo "jp_pisowifi table NOT loaded"; rc=1; }
echo "-- services"
for s in jp-pisowifi-ap jp-pisowifi-network jp-pisowifi jp-pisowifi-monitor jp-pisowifi-coin dnsmasq; do printf '%-24s %s\n' "$s" "$(systemctl is-active $s)"; done
[ $rc -eq 0 ] && echo "VALIDATION OK (note: blocking/bandwidth must still be confirmed from a real customer device, see tests/hardware/checklist.md)" || echo "VALIDATION FOUND PROBLEMS"
exit $rc
