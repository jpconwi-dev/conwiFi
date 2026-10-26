#!/usr/bin/env bash
# Prints "WAN=<iface> LAN=<iface>" when exactly one built-in and one USB Ethernet interface exist.
# Otherwise prints nothing and exits 1 (the installer then asks you to choose).
set -u
builtin=(); usb=()
for p in /sys/class/net/*; do
  n="$(basename "$p")"
  [ "$n" = lo ] && continue
  [ -e "$p/device" ] || continue
  [ -e "$p/wireless" ] && continue
  if readlink -f "$p/device" | grep -q '/usb'; then usb+=("$n"); else builtin+=("$n"); fi
done
if [ "${#builtin[@]}" -eq 1 ] && [ "${#usb[@]}" -eq 1 ]; then
  echo "WAN=${builtin[0]} LAN=${usb[0]}"; exit 0
fi
exit 1
