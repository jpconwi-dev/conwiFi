# Changelog

## 0.2.1
- GPIO input supports libgpiod v2 (Debian 13 trixie, the image on the first real board) as well as v1; tested only against fake modules.
- Real GitHub repo URL set in README and example config.

## 0.2.0 - redesigned portal, pause, pictures and sounds (still untested on hardware)
- Portal rebuilt mobile-first for iOS and Android: safe-area insets, dynamic viewport height, 48 px touch targets,
  16 px form text (no iOS zoom), readable text colour chosen automatically, no inline script/style (strict CSP),
  in-page confirmation instead of browser pop-ups (captive-portal mini browsers block them).
- Pause / resume: time is frozen and internet removed while paused; admin sets whether pause is allowed and the longest pause.
  A coin inserted while paused adds time and resumes.
- Admin "Pictures & sounds": pictures with caption/link/placement (start screen, dashboard or both), editable dashboard
  title and message, per-event sound effects (coin, connected, almost out of time, time up, paused, resumed) with volume
  and a customer mute button.
- Production clean-up: no test coins, simulation banners or `sim-coin` command outside simulation/test mode; removed the
  redundant "Check connection" button and empty/always-"no limit" rows.
- Migration 3 (additive): session pause columns and the media table.

## 0.1.0 - first working version (not yet tested on hardware)
- Flask backend, SQLite (WAL, migrations 1-2), customer portal, admin dashboard.
- Coin service: pulse grouping, debounce, pulse-to-value map, simulation, libgpiod input (disabled until verified).
- Payments: single coin slot claim, atomic crediting, duplicate protection, unassigned-coin tracking.
- Network: nftables captive portal + MAC authorization set with timeouts, dnsmasq DHCP, tc bandwidth limits.
- Recovery: clock-trust check, startup reconcile, expiry, maintenance mode.
- Backups (online), restore, release-based updater with symlink switch and rollback.
- Installer, systemd services, first-boot script (image not built).
- WiFi name label (default conwiFi), editable in Branding.
- coin.test_mode: simulated coin buttons with real networking, for pre-wiring tests on the board.
- 84 automated tests (before 0.2.0) (simulation only; see docs/TESTING.md).
