#!/usr/bin/env bash
# JP-PisoWiFi installer for Armbian/Debian on Orange Pi One. Safe to run repeatedly (idempotent).
# Usage: sudo ./installer/install.sh [--yes] [--wan IFACE] [--lan IFACE] [--simulation] [--force]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(cat "$REPO_DIR/VERSION")"
INSTALL_DIR=/opt/jp-pisowifi
DATA_DIR=/var/lib/jp-pisowifi
CONF_DIR=/etc/jp-pisowifi
CONF=$CONF_DIR/config.json
VENV=$INSTALL_DIR/venv
LOG=/var/log/jp-pisowifi-install.log
SVC_USER=jp-pisowifi
ASSUME_YES=0; FORCE=0; SIM=0; WAN=""; LAN=""

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1;; --force) FORCE=1;; --simulation) SIM=1;;
    --wan) WAN="$2"; shift;; --lan) LAN="$2"; shift;;
    -h|--help) sed -n '2,3p' "$0"; exit 0;;
    *) echo "Unknown option $1"; exit 2;;
  esac; shift
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo ./installer/install.sh"; exit 1; }
mkdir -p "$(dirname "$LOG")"; exec > >(tee -a "$LOG") 2>&1
echo "=== JP-PisoWiFi $VERSION install $(date -Is) ==="

say()  { echo; echo "--> $*"; }
ask()  { [ "$ASSUME_YES" -eq 1 ] && return 0; read -r -p "$1 [y/N] " a; [[ "$a" =~ ^[Yy] ]]; }
fail() { echo "ERROR: $*"; echo "See $LOG. Nothing about your existing WAN connection was changed."; exit 1; }

# 1. verify OS/board ---------------------------------------------------------
say "Checking operating system and board"
set +e; "$REPO_DIR/installer/detect_hardware.sh"; rc=$?; set -e
[ $rc -eq 1 ] && fail "Unsupported operating system (need Debian/Armbian)."
if [ $rc -eq 2 ]; then ask "This does not look like an Orange Pi One. Continue anyway?" || fail "Aborted by user."; fi
mem=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
[ "$mem" -ge 400 ] || fail "Not enough RAM ($mem MB)."
[ "$(df --output=avail -m / | tail -1)" -ge 500 ] || fail "Less than 500 MB free on /."

# 2. interfaces --------------------------------------------------------------
say "Selecting network interfaces"
if [ "$SIM" -eq 0 ]; then
  if [ -z "$WAN" ] || [ -z "$LAN" ]; then
    if out="$("$REPO_DIR/installer/detect_network.sh")"; then
      eval "$out"; echo "Auto-detected WAN=$WAN (built-in), LAN=$LAN (USB)"
      ask "Use these?" || fail "Re-run with --wan IFACE --lan IFACE."
    else
      fail "Could not unambiguously detect interfaces. Re-run with --wan <builtin> --lan <usb-adapter>."
    fi
  fi
  [ -d "/sys/class/net/$WAN" ] || fail "WAN interface $WAN does not exist"
  [ -d "/sys/class/net/$LAN" ] || fail "LAN interface $LAN does not exist"
  [ "$WAN" != "$LAN" ] || fail "WAN and LAN must be different"
  # never cut the branch we are sitting on
  if [ -n "${SSH_CONNECTION:-}" ] && [ "$FORCE" -eq 0 ]; then
    cip="$(echo "$SSH_CONNECTION" | awk '{print $1}')"
    via="$(ip route get "$cip" 2>/dev/null | grep -o 'dev [^ ]*' | head -1 | cut -d' ' -f2 || true)"
    [ "$via" != "$LAN" ] || fail "You are connected over $LAN. SSH in through the WAN interface ($WAN) instead (or use --force)."
  fi
fi

# 3. packages ----------------------------------------------------------------
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip python3-libgpiod nftables dnsmasq iproute2 \
    conntrack git ethtool curl rsync ca-certificates >/dev/null
grep -rqs '^conf-dir=/etc/dnsmasq.d' /etc/dnsmasq.conf || echo "conf-dir=/etc/dnsmasq.d/,*.conf" >> /etc/dnsmasq.conf
systemctl enable nftables >/dev/null 2>&1 || true

# 4. service account ---------------------------------------------------------
say "Creating service account"
getent group gpio >/dev/null || groupadd --system gpio
id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
usermod -aG gpio "$SVC_USER"
install -m 0644 "$REPO_DIR/system/udev/99-jp-pisowifi-gpio.rules" /etc/udev/rules.d/
udevadm control --reload-rules 2>/dev/null || true

# 5. application files + venv -------------------------------------------------
say "Installing application $VERSION"
REL="$INSTALL_DIR/releases/v$VERSION"
mkdir -p "$INSTALL_DIR/releases"
rsync -a --delete --exclude '.git' --exclude 'data' --exclude '__pycache__' --exclude 'config/config.local.json' \
      "$REPO_DIR/" "$REL/"
[ -d "$VENV" ] || python3 -m venv --system-site-packages "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$REL/requirements.txt"
ln -sfn "$REL" "$INSTALL_DIR/current.new" && mv -T "$INSTALL_DIR/current.new" "$INSTALL_DIR/current"
install -m 0755 "$REL/scripts/jp-pisowifi" /usr/local/bin/jp-pisowifi

# 6. persistent dirs + config (never overwritten) ------------------------------
say "Creating directories and configuration"
mkdir -p "$DATA_DIR" "$CONF_DIR"
chown "$SVC_USER:$SVC_USER" "$DATA_DIR"; chmod 2775 "$DATA_DIR"
if [ -f "$CONF" ]; then
  echo "Keeping existing $CONF"
else
  SIMFLAG=$([ "$SIM" -eq 1 ] && echo true || echo false)
  "$VENV/bin/python" - "$CONF" "$DATA_DIR" "$SIMFLAG" "$WAN" "$LAN" <<'PY'
import json, sys
path, data, sim, wan, lan = sys.argv[1:6]
cfg = json.load(open("/opt/jp-pisowifi/current/config/config.example.json"))
cfg["data_dir"] = data
cfg["simulation_mode"] = (sim == "true")
cfg["network"]["wan_iface"], cfg["network"]["lan_iface"] = wan, lan
json.dump(cfg, open(path, "w"), indent=2)
PY
  chmod 0644 "$CONF"
fi

# 7. rollback snapshot ---------------------------------------------------------
say "Saving a network snapshot for rollback"
SNAP="$DATA_DIR/rollback/$(date +%Y%m%d-%H%M%S)"; mkdir -p "$SNAP"
{ ip -br addr; echo; ip route; } > "$SNAP/ip.txt" 2>&1 || true
nft list ruleset > "$SNAP/nft.txt" 2>&1 || true
ls /etc/NetworkManager/conf.d /etc/systemd/network /etc/netplan 2>/dev/null > "$SNAP/netconf-files.txt" || true
echo "Snapshot: $SNAP   (restore firewall to defaults any time with: sudo jp-pisowifi firewall-restore)"

# 8. keep NetworkManager away from the LAN interface ------------------------------
if [ "$SIM" -eq 0 ] && systemctl is-active --quiet NetworkManager 2>/dev/null; then
  LANMAC="$(cat /sys/class/net/$LAN/address)"
  NMCONF=/etc/NetworkManager/conf.d/99-jp-pisowifi-unmanaged.conf
  if [ ! -f "$NMCONF" ]; then
    echo "NetworkManager is running. It must ignore the customer LAN interface ($LAN, $LANMAC)."
    ask "Create $NMCONF and reload NetworkManager?" || fail "Cannot continue without it (or re-run with --simulation)."
    printf '[keyfile]\nunmanaged-devices=mac:%s\n' "$LANMAC" > "$NMCONF"
    nmcli general reload conf 2>/dev/null || systemctl reload NetworkManager || true
  fi
fi
cat > /etc/sysctl.d/99-jp-pisowifi.conf <<'SYS'
net.ipv4.ip_forward=1
SYS
sysctl -q -p /etc/sysctl.d/99-jp-pisowifi.conf || true

# 9. database + admin ------------------------------------------------------------
say "Initialising database (existing data is never touched)"
cd "$REL"
JP_PISOWIFI_CONFIG=$CONF "$VENV/bin/python" -m backend.cli init  || fail "database init failed"
JP_PISOWIFI_CONFIG=$CONF "$VENV/bin/python" -m backend.cli migrate

# 10. services -----------------------------------------------------------------
say "Installing systemd services"
for u in "$REL"/system/systemd/jp-pisowifi*.service "$REL"/system/systemd/jp-pisowifi-update.path; do
  install -m 0644 "$u" /etc/systemd/system/
done
systemctl daemon-reload
if [ "$SIM" -eq 0 ]; then systemctl enable --now jp-pisowifi-network.service || echo "WARNING: network service failed; run: journalctl -u jp-pisowifi-network"; fi
systemctl enable --now jp-pisowifi.service jp-pisowifi-monitor.service jp-pisowifi-update.path
systemctl enable jp-pisowifi-coin.service
systemctl restart jp-pisowifi.service jp-pisowifi-monitor.service
systemctl restart jp-pisowifi-coin.service || true   # exits cleanly (code 3) until GPIO is verified, see docs/HARDWARE.md

# 11. health checks ------------------------------------------------------------
say "Running health checks"
sleep 4
"$REL/installer/validate_install.sh" || true

cat <<MSG

=============================================================
 JP-PisoWiFi $VERSION installed.
 Admin panel : http://<this Pi's WAN IP>/admin   (not reachable from customer WiFi by default)
 Credentials : shown above and in $DATA_DIR/INITIAL_ADMIN_CREDENTIALS.txt (change on first login, then delete the file)
 Coin input  : DISABLED until you verify wiring and set coin.gpio_line / coin.hardware_verified
               in $CONF (docs/HARDWARE.md). Use the portal's simulated coin buttons only if simulation_mode=true.
 Useful      : sudo jp-pisowifi status | sudo systemctl status jp-pisowifi
               sudo journalctl -u jp-pisowifi -n 100 --no-pager
               sudo jp-pisowifi firewall-restore   (removes our firewall rules if something goes wrong)
 Install log : $LOG
=============================================================
MSG
