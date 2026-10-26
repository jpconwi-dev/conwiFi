# Hardware validation checklist (fill in on the real Orange Pi)
Record date, Armbian version, result. Do not mark anything passed unless you ran it.

- [ ] `installer/detect_hardware.sh` output saved
- [ ] Installer completes; `sudo jp-pisowifi status` HEALTHY
- [ ] Phone joins WiFi, gets 10.0.0.x address from the Pi (AP's own DHCP is off; `sudo journalctl -u dnsmasq`)
- [ ] Before paying: phone gets the portal prompt; `curl http://example.com` from the phone shows the portal; HTTPS sites fail
- [ ] Before paying: phone cannot reach `http://<Pi WAN IP>/admin` (expect 404)
- [ ] After a simulated coin (`sudo jp-pisowifi sim-coin 5` only valid in simulation) or real coin: internet works within ~3 s
- [ ] `sudo nft list set inet jp_pisowifi authorized` shows the phone's MAC with a timeout
- [ ] At expiry the phone loses internet; existing downloads stop
- [ ] Speed limit: `speedtest` on phone matches limit; `tc -s class show dev <lan>`
- [ ] Two phones at once, different limits
- [ ] Power cut during a session; after boot (no internet): session restored only if time remains; clock-untrusted case: customers stay blocked until `trust-clock` or NTP
- [ ] Unplug WAN: portal still opens; internet status shows offline; sessions keep counting
- [ ] `kill -9` of gunicorn / monitor: restarts within seconds
- [ ] Coin wiring measured with multimeter BEFORE connecting; opto circuit built; each coin value counted correctly; rapid coins
- [ ] Backup, restore, update to a tagged release, and a deliberately broken release (rollback)
