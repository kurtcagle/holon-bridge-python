# restart_fuseki: host setup

The bridge does not run as root and never calls `systemctl`. It writes a request file; a root
helper does the restart. Design and rules: `docs/restart-fuseki.spec.md` and
`holonbridge/fuseki_restart.py`.

## Install (on the instance that runs Fuseki)

```bash
sudo install -d -m 0750 -o <bridge-user> -g <bridge-user> /var/lib/holonbridge
sudo install -m 0755 holonbridge-restart-fuseki /usr/local/sbin/
sudo install -m 0644 holonbridge-restart-fuseki.service holonbridge-restart-fuseki.path /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now holonbridge-restart-fuseki.path
```

`/etc/holonbridge/host.conf` (read by the helper) needs `FUSEKI_CONFIG_DIR`, the same directory
the bridge uses. Optional: `FUSEKI_RESTART_DIR` (default `/var/lib/holonbridge`),
`FUSEKI_SERVICE` (`fuseki.service`), `FUSEKI_PING_URL`, `HEALTH_WAIT` (60 s).

## Bridge settings

| Variable | Meaning |
|---|---|
| `FUSEKI_RESTARTERS` | external identifiers allowed to restart Fuseki (empty = off) |
| `FUSEKI_RESTART_DIR` | the directory above; unset = off |
| `FUSEKI_CONFIG_DIR` | Fuseki's configuration directory (existing setting) |
| `FUSEKI_RESTART_MIN_INTERVAL` | seconds between restarts (default 60) |
| `SNAPSHOT_DIR` | where snapshots and the pinned default backup live |
| `FUSEKI_BACKUP_DIR` | the nightly backup's folder; `SNAPSHOT_DIR` may not be inside it |

## Bridge unit dependency

`holonbridge.service` must use `Wants=fuseki.service` (with `After=fuseki.service`), not
`Requires=`. With `Requires=`, restarting Fuseki stops the bridge first, and the bridge is the
process waiting for the helper's result: its shutdown hangs until systemd's stop timeout (about
90 s), the call is dropped, and in-memory datasets are never rebuilt. After changing the line,
`systemctl daemon-reload`; no restart is needed.

## Rollback

After every restart that comes back healthy, the helper copies Fuseki's configuration to
`last-known-good/`. If a restart does not come back healthy within `HEALTH_WAIT`, it puts that
copy back and restarts again, and reports `rolledBack`. The very first restart has nothing to
roll back to.

## Nightly backup

Not changed by this feature, except for one optional line so the bridge's own snapshots reach
S3 (they are NOT in `$BK`, which is all the script syncs today):

```bash
aws s3 sync "$BRIDGE_DIR/backups" "s3://$BUCKET/bridge-backups/" --only-show-errors
```
