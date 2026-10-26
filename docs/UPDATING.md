# Releases and updates

`sudo jp-pisowifi update [--version vX.Y.Z]` (or the System page) does:
1. refuses unless `update.repo_url` is an `https://` URL (not the placeholder);
2. lists tags, picks the highest `vMAJOR.MINOR.PATCH` (or the one you named);
3. takes a database backup (`...pre-update...db`);
4. shallow-clones that tag into `/opt/jp-pisowifi/releases/vX.Y.Z` (optionally requires a signed tag: `update.require_signed_tag`);
5. installs its `requirements.txt` into the shared venv, runs its migrations;
6. atomically switches `/opt/jp-pisowifi/current`, restarts services, runs the health check;
7. on a failed health check switches back to the previous release and restarts.
Config (`/etc/jp-pisowifi/config.json`) and data (`/var/lib/jp-pisowifi`) are never replaced.

**Migration rule:** migrations must be additive (new tables/columns/indexes; no drops or renames inside one release) so the previous
release still works on the upgraded database when a rollback happens. Rollback restores code, not data; use the pre-update backup only if needed.
A shared venv means a rollback can't undo a dependency upgrade: pin conservatively.

Publish: set `VERSION`, update `CHANGELOG.md`, `git tag -s vX.Y.Z` (or `git tag vX.Y.Z`), `git push --tags`.
