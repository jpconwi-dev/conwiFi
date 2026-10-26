#!/usr/bin/env bash
[ -n "${1:-}" ] || { echo "usage: restore.sh /var/lib/jp-pisowifi/backups/FILE.db"; exit 1; }
exec jp-pisowifi restore "$1"
