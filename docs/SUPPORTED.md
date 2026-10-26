# Supported / targeted environment

Target: Orange Pi One (Allwinner H3, 1 GB), Armbian Minimal/CLI on Debian 12 (Bookworm), Python 3.11, systemd 252.
Development/testing of the simulation mode was done on Python 3.12 (Linux) with Flask 3.x.
Pinned runtime packages are in `requirements.txt`; `python3-libgpiod` comes from apt (Debian's 1.6 bindings).

NOT yet verified: any specific Armbian image, kernel, USB Ethernet chipset, or the nftables features on your kernel.
Run `installer/detect_hardware.sh` and keep its output with your notes. `nft -c -f` is run before every ruleset load so a
syntax problem is refused instead of half-applied.
