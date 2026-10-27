# JP-PisoWiFi

Coin-operated WiFi (PisoWiFi) for an **Orange Pi One H3 (1 GB) on Armbian**: customer portal, admin dashboard, coin acceptor service,
SQLite accounting, nftables access control, dnsmasq DHCP, tc speed limits, systemd auto-recovery and a release-based updater.

> **Status: v0.1.0, tested in simulation only.** The automated tests pass on a development machine. Nothing has been run on an Orange Pi,
> with real nftables/tc/dnsmasq, a phone, or a coin acceptor. Follow `tests/hardware/checklist.md` before charging customers.
> The coin input is intentionally disabled until you verify the wiring (`docs/HARDWARE.md`).

## WiFi options
Either an external router in AP/bridge mode with a USB Ethernet adapter, or (new in 0.3.0) a USB WiFi adapter such as the Alfa AWUS036ACM used as the access
point by the Pi itself (`sudo bash installer/install.sh --wifi-ap`), which lets the admin panel change the real SSID, password and channel. See `docs/NETWORK.md`.

## Customer experience
Mobile-first portal (iOS Safari and its captive-portal mini browser, Android Chrome): big countdown ring, one-tap Insert coin / Add time,
Pause and Resume, rates as cards, swipeable pictures chosen by you, optional sound effects with a mute button. Admin controls everything
under **Pictures & sounds** (pictures, dashboard title/message, sounds) and **System** (pause on/off and longest pause).
Phones only allow audio after the first tap on the page, and iPhones cannot play OGG: use MP3 or M4A.

## Architecture
```
Internet - source router - [Pi built-in Ethernet = WAN] Orange Pi [USB Ethernet = LAN] - WiFi router (AP/bridge) - customers
```
| Service | Runs as | Job |
|---|---|---|
| `jp-pisowifi` (gunicorn + Flask) | `jp-pisowifi` | portal, admin panel, writes to SQLite only |
| `jp-pisowifi-monitor` | root | every 2 s: expires sessions, syncs the nftables `authorized` set and tc limits from SQLite, internet check, backups, network rollback timer |
| `jp-pisowifi-coin` | `jp-pisowifi` (+`gpio`) | reads coin pulses, credits payments |
| `jp-pisowifi-network` | root (oneshot) | LAN address, forwarding, firewall, dnsmasq |
| `jp-pisowifi-update.path` | root | runs the updater when the admin panel requests it |

The web app never changes the firewall; only the monitor does, so a bug or attack on the portal cannot open the network.

### How a customer gets online
1. Phone joins WiFi, DHCP from the Pi; HTTP probes are DNAT-ed to the portal.
2. Customer taps **Insert coin**: the single coin slot is reserved for that device for 30 s (`coin.claim_seconds`).
3. Each coin is recorded in one SQLite transaction (coin event, payment, sale, session expiry as an absolute timestamp). Duplicate event ids never credit twice.
4. The monitor adds the device MAC to the kernel set with a timeout equal to the remaining time. Expiry removes it and flushes connections.
Coins dropped while nobody holds the slot are stored as *unassigned* (visible in Sales), never silently lost or credited to the wrong person.

### Power cuts
Expiry timestamps live in SQLite. The Orange Pi One has **no real-time clock**, so after power loss the clock may restart in the past, which would give free time.
The monitor records a heartbeat every cycle; if the clock is behind it (or before `min_valid_epoch`) all customers are blocked and nothing is deleted until the time is sane
(NTP, or confirm manually with `sudo jp-pisowifi trust-clock`). Power-off time counts against sessions. A DS3231 RTC module or a small UPS is strongly recommended.

## Quick start on Windows (simulation, PowerShell)
Keep the project outside OneDrive if you can (SQLite files and cloud sync do not mix); at minimum point `data_dir` outside it as below.
```
cd C:\path\to\JP-PisoWiFi
python -m venv .venv
.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
pip install -r requirements.txt
New-Item -ItemType Directory C:\jp-data -Force
Copy-Item config\config.example.json config\config.local.json
(Get-Content config\config.local.json) -replace '"data_dir": "./data"','"data_dir": "C:/jp-data"' | Set-Content config\config.local.json
python -m backend.cli init           # prints the admin password
python -c "from backend.app import create_app; create_app().run(port=8080)"
```
Open http://127.0.0.1:8080/ (portal, with simulated coin buttons; add `?sim=2` to act as another customer) and http://127.0.0.1:8080/admin.
Run tests: `python -m unittest discover -s tests -p "test_*.py" -t .`
(Windows PowerShell 5.1 has no `&&`; run the commands one per line.)

## Install on the Orange Pi
```
sudo apt install -y git
git clone https://github.com/jpconwi-dev/conwiFi.git
cd conwiFi
sudo bash installer/install.sh        # add --wan <builtin> --lan <usb> if detection is ambiguous
```
Read `docs/NETWORK.md` first (AP setup, SSH over the WAN side, subnet conflicts). Installer options: `--yes`, `--simulation`, `--force`.
It is idempotent: it never overwrites `/etc/jp-pisowifi/config.json`, the database, or credentials; re-running refreshes code and units.
Useful commands: `sudo jp-pisowifi status|health|backup|restore FILE|update|firewall-restore|reset-admin-password|trust-clock`,
`sudo systemctl status jp-pisowifi`, `sudo journalctl -u jp-pisowifi -n 100 --no-pager`.

## Repository layout (differs slightly from the original plan)
```
installer/   install.sh first_boot.sh detect_hardware.sh detect_network.sh validate_install.sh uninstall.sh
             (network setup and service configuration are inside install.sh and backend/cli.py network-up)
backend/     app.py config.py database.py auth.py coin_service.py coin_daemon.py payment_service.py session_service.py
             client_service.py network_service.py bandwidth_service.py reporting_service.py recovery_service.py
             backup_service.py updater.py monitor.py cli.py clock.py
frontend/    templates/ (portal, client_dashboard partial, admin pages) static/ (css, js)
system/      systemd/ udev/ dnsmasq/ nftables/ captive-portal/      database/ schema.sql + migrations/
scripts/     jp-pisowifi (CLI wrapper) backup.sh restore.sh status.sh health_check.sh update.sh recovery.sh
tests/       unit/ integration/ hardware/checklist.md          docs/  HARDWARE NETWORK UPDATING SUPPORTED TESTING IMAGE
```
Clients and Sessions share one admin page; Reports are part of Sales.

## Security notes
scrypt password hashes, forced password change for the generated initial admin, DB-backed login lockout (5 failures / 15 min per IP+user), per-session CSRF tokens on every POST,
CSP without inline script/style, strict logo upload sniffing, parametrised SQL, audit log of admin changes, admin panel hidden from customer-side devices, SSH only from WAN side,
no shell execution from the dashboard (update is a fixed command triggered by a flag file). Cookies are not `Secure` because the LAN portal is plain HTTP; the admin panel should
be used from the trusted management side. Never commit `config.local.json`, `data/`, `*.db` (all in `.gitignore`).

## Known limitations and untested areas
- Not run on hardware: nftables rules (only generated and string-checked), DNAT portal redirect, dnsmasq, tc shaping, libgpiod input, systemd ordering, `nft -c` on your kernel.
- Customer devices are identified by MAC; phones with randomised MACs can look like new devices. HTTPS cannot be redirected; the portal depends on the phone's HTTP probe.
- Bandwidth limits are per IP and applied when the monitor next syncs; speed shown to customers is the configured limit, not a live measurement.
- Admin "restore" is CLI-only on purpose. The updater's rollback after a failed health check is implemented but not exercised by tests.
- NoDogSplash was not integrated (see `system/captive-portal/README.md`). IPv6 is not routed to customers.
- No bootable image yet (`docs/IMAGE.md`). A shared Python venv means dependency changes can't be rolled back with the code.
