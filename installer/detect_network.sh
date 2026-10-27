#!/usr/bin/env bash
# Prints "WAN=<iface> LAN=<iface> MODE=<ethernet|wifi>" when the layout is unambiguous, else exits 1.
#   MODE=ethernet : exactly one built-in Ethernet + one USB Ethernet adapter
#   MODE=wifi     : exactly one built-in Ethernet + one WiFi adapter and no USB Ethernet (or "--wifi-ap" given)
set -u
force_wifi=0; [ "${1:-}" = "--wifi-ap" ] && force_wifi=1
builtin=(); usb=(); wifi=(); usbwifi=()
for p in /sys/class/net/*; do
  n="$(basename "$p")"
  [ "$n" = lo ] && continue
  isusb=0; readlink -f "$p/device" 2>/dev/null | grep -q '/usb' && isusb=1
  if [ -e "$p/wireless" ] || [ -e "$p/phy80211" ]; then
    wifi+=("$n"); [ $isusb -eq 1 ] && usbwifi+=("$n"); continue
  fi
  [ -e "$p/device" ] || continue
  if [ $isusb -eq 1 ]; then usb+=("$n"); else builtin+=("$n"); fi
done
[ "${#builtin[@]}" -eq 1 ] || exit 1
cand=("${wifi[@]}"); [ "${#usbwifi[@]}" -ge 1 ] && cand=("${usbwifi[@]}")
if [ $force_wifi -eq 1 ] || { [ "${#usb[@]}" -eq 0 ] && [ "${#wifi[@]}" -ge 1 ]; }; then
  [ "${#cand[@]}" -eq 1 ] || exit 1
  echo "WAN=${builtin[0]} LAN=${cand[0]} MODE=wifi"; exit 0
fi
if [ "${#usb[@]}" -eq 1 ]; then echo "WAN=${builtin[0]} LAN=${usb[0]} MODE=ethernet"; exit 0; fi
exit 1
