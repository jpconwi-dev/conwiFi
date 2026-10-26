# Changelog

## 0.1.0 - first working version (not yet tested on hardware)
- Flask backend, SQLite (WAL, migrations 1-2), customer portal, admin dashboard.
- Coin service: pulse grouping, debounce, pulse-to-value map, simulation, libgpiod input (disabled until verified).
- Payments: single coin slot claim, atomic crediting, duplicate protection, unassigned-coin tracking.
- Network: nftables captive portal + MAC authorization set with timeouts, dnsmasq DHCP, tc bandwidth limits.
- Recovery: clock-trust check, startup reconcile, expiry, maintenance mode.
- Backups (online), restore, release-based updater with symlink switch and rollback.
- Installer, systemd services, first-boot script (image not built).
- WiFi name label (default conwiFi), editable in Branding.
- 82 automated tests (simulation only; see docs/TESTING.md).
