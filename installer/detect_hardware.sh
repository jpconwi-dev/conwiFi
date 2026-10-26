#!/usr/bin/env bash
# Prints a hardware/OS report. Exit code 0 = looks supported, 1 = unsupported OS, 2 = supported OS but unexpected board.
set -u
model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
arch="$(uname -m)"
kernel="$(uname -r)"
. /etc/os-release 2>/dev/null || true
armbian="$(. /etc/armbian-release 2>/dev/null; echo "${VERSION:-not-armbian} (board=${BOARD:-?}, branch=${BRANCH:-?})")"
mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
disk="$(df -h / | awk 'NR==2 {print $4" free of "$2}')"
echo "Board model     : $model"
echo "CPU arch        : $arch"
echo "Kernel          : $kernel"
echo "OS              : ${PRETTY_NAME:-unknown} (debian base: $(cat /etc/debian_version 2>/dev/null || echo n/a))"
echo "Armbian         : $armbian"
echo "Memory          : ${mem_mb} MB"
echo "Root storage    : $disk"
echo "GPIO chips      : $(ls /dev/gpiochip* 2>/dev/null | tr '\n' ' ' || true)"
echo "I2C devices     : $(ls /dev/i2c-* 2>/dev/null | tr '\n' ' ')  (pin 3/5 are I2C0; if listed, I2C is enabled and pin 3 is NOT free for GPIO)"
echo "Overlays        : $(grep -E '^overlays=' /boot/armbianEnv.txt 2>/dev/null || echo 'none/unknown')"
echo "Ethernet ifaces :"
for p in /sys/class/net/*; do
  n="$(basename "$p")"
  [ "$n" = lo ] && continue
  [ -e "$p/device" ] || continue
  [ -e "$p/wireless" ] && continue
  bus="platform/built-in"; readlink -f "$p/device" | grep -q '/usb' && bus="USB"
  drv="$(basename "$(readlink -f "$p/device/driver" 2>/dev/null)" 2>/dev/null)"
  echo "   $n  mac=$(cat "$p/address")  type=$bus  driver=${drv:-?}  state=$(cat "$p/operstate")"
done
case "${ID:-}" in debian|ubuntu|armbian) ;; *) echo "UNSUPPORTED: OS id '${ID:-unknown}'"; exit 1;; esac
case "$arch" in armv7l|aarch64|armv8l) ;; *) echo "NOTE: unexpected architecture $arch (target is armv7l)";; esac
echo "$model" | grep -qi "orange pi one" || { echo "NOTE: board is not reported as an Orange Pi One."; exit 2; }
exit 0
