# Testing

```
python -m unittest discover -s tests -p "test_*.py" -t .
```
Runs on Windows/Linux with only Flask installed (simulation mode: no root, nft, tc or GPIO). Covered: pulse parsing/debounce, denomination mapping,
duplicate protection, accounting, rates, expiry/extension, admin auth + lockout + CSRF, XSS/upload checks, migrations (incl. rollback of a failed one),
reboot/clock-reset recovery, maintenance/blocked clients, backup/restore/retention, ruleset/dnsmasq/tc *generation*, release updater (local git repo),
admin hiding from customer LAN addresses, captive redirect.

**Not covered by automated tests (needs the real board):** that nftables actually blocks/allows traffic, DNAT portal redirect on phones,
dnsmasq DHCP, tc shaping, GPIO pulses, systemd boot ordering, power-cut behaviour, the updater's rollback-after-failed-health-check branch, `nft -c` acceptance
on your kernel. See `tests/hardware/checklist.md`.
