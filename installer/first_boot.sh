#!/usr/bin/env bash
# First-boot provisioning for a prepared image (image NOT built/tested yet; see docs/IMAGE.md).
# Does only what can be done safely: if interfaces are ambiguous it stops and writes instructions.
set -u
CONF=/etc/jp-pisowifi/config.json
PY=/opt/jp-pisowifi/venv/bin/python
STATUS=/var/lib/jp-pisowifi/readiness.txt
mkdir -p /var/lib/jp-pisowifi /etc/jp-pisowifi
exec >>"$STATUS" 2>&1
echo "=== first boot $(date -Is) ==="
cd /opt/jp-pisowifi/current || exit 1
/opt/jp-pisowifi/current/installer/detect_hardware.sh
if [ ! -f "$CONF" ]; then
  if out="$(bash /opt/jp-pisowifi/current/installer/detect_network.sh)"; then
    eval "$out"
    $PY - "$CONF" "$WAN" "$LAN" "${MODE:-ethernet}" <<'PY'
import json, sys
cfg = json.load(open("/opt/jp-pisowifi/current/config/config.example.json"))
cfg["data_dir"] = "/var/lib/jp-pisowifi"; cfg["simulation_mode"] = False
cfg["network"]["wan_iface"], cfg["network"]["lan_iface"] = sys.argv[2], sys.argv[3]
cfg["network"]["lan_mode"] = sys.argv[4]
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PY
  else
    echo "STOPPED: could not tell which Ethernet port is WAN and which is LAN."
    echo "Create $CONF with network.wan_iface / network.lan_iface (see docs/NETWORK.md) then reboot."
    exit 0
  fi
fi
JP_PISOWIFI_CONFIG=$CONF $PY -m backend.cli init
JP_PISOWIFI_CONFIG=$CONF $PY -m backend.cli migrate
touch /var/lib/jp-pisowifi/.provisioned
echo "READY: services start next. Coin input stays disabled until wiring is verified (docs/HARDWARE.md)."
