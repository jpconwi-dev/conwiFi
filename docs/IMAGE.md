# Plug-and-play image (NOT built)

No bootable image has been produced or tested. The pieces that exist: `installer/first_boot.sh` and
`system/systemd/jp-pisowifi-firstboot.service` (runs once, writes `/var/lib/jp-pisowifi/readiness.txt`).
Recommended route when the install works on your board: use Armbian's `build` framework with a userpatches `customize-image.sh` that
copies this repo to `/opt/jp-pisowifi/releases/v<version>`, creates the venv, installs the units and enables the first-boot unit. Flash with
balenaEtcher, then boot and read `readiness.txt`. Steps requiring a real board: interface detection, USB adapter driver, GPIO verification.
