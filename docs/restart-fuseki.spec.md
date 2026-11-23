---
title: "restart_fuseki: admin tool to restart the Fuseki backend from the HolonBridge MCP"
status: draft 7 (decisions 11 to 13 accepted; implementation details to confirm on the instance; decision 4 deferred)
date: 2026-11-22
applies-to: kurtcagle/holon-bridge-python (holonbridge/, routes/), holonbridge.dev MCP
---

# `restart_fuseki`: restart the Fuseki backend from the HolonBridge MCP

## 1. Why this exists

On 2026-11-22 `update_fluent` on `ds:worldtest` failed with a 405 on `/worldtest/shacl`. The cause is by design, and is documented in `service_config.py`: Fuseki's admin API creates a dataset from a fixed template that has no `fuseki:shacl` endpoint. The bridge can append one to the dataset's assembler file (`{FUSEKI_CONFIG_DIR}/{name}.ttl`), but Fuseki reads that file only at startup. Until Fuseki restarts, the dataset has no SHACL endpoint, so every SHACL-gated write fails.

The bridge could not restart Fuseki, and neither could the assistant (the instance role has no `ssm:SendCommand`). The restart needed a person with a shell on the box. Two other flows leave the same gap: `provision_text_index` (Lucene, `restart_required: true`) and any dataset created through `create_dataset`.

**Goal:** a narrow, admin-only, audited tool that restarts the Fuseki service, brings any in-memory datasets back from backup, and confirms everything returned, so these flows finish without a shell session.

## 2. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Reverse the stance in `provisioning.py` and `routes/dataset_admin.py` that the bridge "never restarts" Fuseki. | **Accepted** 2026-11-22 |
| 2 | Detect in-memory datasets by parsing the assembler files, not by one fixed shape (section 4.4). | **Accepted** |
| 3 | Automatic rollback of a failed restart is part of the first version (section 6). | **Accepted** |
| 4 | Per-bank `store_kind` (section 9). | **Deferred**; revisit before the first Fluree test |
| 5 | In-memory datasets are reconstructed from their default or latest backup, not refused. Backups warn about them and offer to include them (sections 4.4, 5). | **Accepted** |
| 6 | Backups live in a `backups` subfolder of the holon-bridge directory (`SNAPSHOT_DIR`). A dataset's *default* backup is a pinned copy in `backups/{dataset}/default/` (section 4.4). | **Accepted** 2026-11-22 |
| 7 | The nightly backup script already dumps every dataset the server lists, in-memory ones included, through Fuseki's backup API, so no extra pre-step is needed to cover them. The warning stays for the bridge's own snapshot and export operations (section 5). | **Accepted**; revised after reading the script |
| 8 | Minimum interval between restarts is 60 seconds, configurable (section 4.2). | **Accepted** |
| 9 | `create_dataset` runs the restart dry run itself and returns `restart_required` plus the plan (section 8). | **Accepted** |
| 10 | `create_dataset` also accepts an opt-in `restart: "commit"` so the whole flow ends in one call. The default stays `"plan"` (section 8). | **Accepted** 2026-11-22 |
| 11 | The newest nightly dump is a third restore source for an in-memory dataset, alongside the pinned default and the bridge snapshots (section 4.4). | **Accepted** 2026-11-22 |
| 12 | The nightly script gains a second `aws s3 sync` for the bridge snapshot folder, so pinned defaults and pre-restart snapshots are in S3 (section 5). | **Accepted** 2026-11-22 |
| 13 | Nightly dumps older than 7 days are fetched from S3 as a last-resort restore source, after local snapshots and local dumps (section 4.4). | **Accepted** 2026-11-22 |

Why the reversal is now safe: per-caller identity reaches the REST layer (the same reason `create_dataset` became possible on 2026-10-27); the tool is gated on an explicit allowlist, not dataset admin; and pre-flight checks remove the two real dangers, a bad assembler file aborting every dataset (2026-09-27) and losing in-memory data.

## 3. Tool contract

### 3.1 MCP tool

```
restart_fuseki(
  reason:           str,              # required; written to the audit log
  commit:           bool = false,     # false = dry run: report the plan, change nothing
  snapshot_first:   bool = true,      # snapshot every non-empty in-memory dataset before restarting
  restore_from:     "auto"|"latest"|"default" = "auto",
  acknowledge_loss: list[str] = [],   # in-memory datasets allowed to restart with no usable backup
  wait_seconds:     int = 90,         # how long to wait for the datasets to return (10..300)
)
```

REST equivalent: `POST /admin/backend/restart`, body as above, in a new `routes/backend_admin.py` next to `dataset_admin.py`.

### 3.2 Dry-run result (`commit=false`, the default)

```json
{
  "store": "fuseki",
  "would_restart": true,
  "blockers": [],
  "datasets": ["admin","chloe","dell","ds","hcg","worldtest","scratch"],
  "in_memory": [
    {
      "dataset": "scratch",
      "empty": false,
      "backup_source": {"kind": "snapshot", "snapshot": "20261122T180003Z", "bytes": 1204331},
      "unsaved_changes": true,
      "will_snapshot_first": true,
      "restorable": true
    }
  ],
  "pending_config": [
    {"dataset": "worldtest", "change": "shacl endpoint declared in config, not served"}
  ],
  "config_preflight": {"files": 7, "ok": 7, "errors": []},
  "in_flight_writes": 0,
  "last_restart": "2026-11-22T19:58:11Z"
}
```

`would_restart` is `false` whenever `blockers` is non-empty. A commit call with blockers is refused with 409 and the same body.

### 3.3 Commit result

```json
{
  "restarted": true,
  "verified": true,
  "duration_ms": 14230,
  "datasets_before": ["admin","chloe","dell","ds","hcg","worldtest","scratch"],
  "datasets_after":  ["admin","chloe","dell","ds","hcg","worldtest","scratch"],
  "services_gained": [{"dataset":"worldtest","service":"shacl"}],
  "services_lost": [],
  "in_memory": [
    {"dataset":"scratch","snapshot":"20261122T203109Z","restored":true,
     "graphs":12,"counts_match":true,"changes_lost":false}
  ],
  "rolled_back": false,
  "audit_id": "urn:...:audit-..."
}
```

If the datasets, services or restored counts do not match after `wait_seconds`, return `restarted: true, verified: false` with the diff and HTTP 200. The restart happened, so this is not an error, but the caller must see the mismatch.

## 4. Guards

### 4.1 Authorisation

- A new setting `FUSEKI_RESTARTERS` (comma-separated external identities, same parser shape as `parse_dataset_creators`). Empty means the tool is off.
- Requires an established identity. The shared bearer token, `x-holon-animus-id` assertions without a proxy token, and `act_as` impersonation are all refused. Restarting is a server-level act: it interrupts every dataset and every user, so admin on one dataset must not imply it.

### 4.2 Refusal conditions (each one is a `blocker`)

| Blocker | Why |
|---|---|
| **An in-memory dataset has no usable backup** | A restart empties it. It is a blocker only if the dataset is non-empty, has no existing snapshot, **and** `snapshot_first` cannot make one (`SNAPSHOT_DIR` unset, dataset over `SNAPSHOT_MAX_BYTES`, or the snapshot fails). Override per dataset with `acknowledge_loss`. An *empty* in-memory dataset has nothing to lose and is never a blocker. |
| **Any assembler file fails pre-flight** | Fuseki aborts startup on one bad file (`readConfigurationDirectory`), taking down every dataset. Pre-flight parses every `*.ttl` in `FUSEKI_CONFIG_DIR` with `turtle.parse` and checks each has exactly one named `fuseki:Service` (the same check as `service_config._service`). |
| **A write is in flight** | Take the existing write lock (`write_lock.py`), drain in-flight fluent transactions, then hold new writes for the duration. |
| **A Fuseki backup task is running** | The nightly script waits on each dump and aborts the whole run if Fuseki stops answering (`set -e`). Check `/$/tasks` for unfinished tasks and refuse until they finish. |
| **Restarted less than 60 seconds ago** | Stops restart loops. Setting `FUSEKI_RESTART_MIN_INTERVAL` (seconds, default 60). Single-flight: a second concurrent call gets 409. |
| **Bank is not local to this host** | The bridge can restart only a service it shares a host with. Condition: the bank's URL host is loopback and `FUSEKI_CONFIG_DIR` is set. |
| **Store is not Fuseki** | See section 9. |

### 4.3 Behaviour during the restart

- New writes get `503` with `retryable: true` and `retry_after_ms`. Reads fail naturally while Fuseki is down. The Worldtest Adventure page already maps `server_unavailable` to a retry.
- The scheduler tick is paused (`scheduler_enabled`) and resumed after verification.
- Caches (named queries, `cache.py`) are cleared after the restart. Per-identity dataset and bank overrides are untouched; they are persisted by the bridge, not Fuseki.

### 4.4 In-memory datasets: detect, back up, restart, reconstruct

**What the source already gives us.** Fuseki keeps an in-memory dataset's *configuration* across a restart, so the dataset reappears, with its shacl endpoint, but empty (`dataset_create.py`, checked live by the author). The bridge already has `snapshot.py`, which snapshots a dataset to files and restores into an empty one. The restart tool builds on those and does not duplicate them.

**Detection (decision 2).** For each file in `FUSEKI_CONFIG_DIR`, parse it and find the dataset the service points at (`fuseki:dataset`). If that is a `text:TextDataset` (as added by `provision_text_index`), follow `text:dataset` to the wrapped one. Then classify:

| Parsed shape | Class |
|---|---|
| `ja:MemoryDataset` | in-memory |
| `tdb2:DatasetTDB2` or `tdb:DatasetTDB` with a `*:location` equal to `"--mem--"` | in-memory |
| `tdb2:DatasetTDB2` or `tdb:DatasetTDB` with any other `*:location` | persistent |
| Anything else, or a dataset listed by `/$/datasets` with no config file | **unknown, treated as in-memory** |

This replaces the earlier question about "the exact assembler shape `create_dataset` writes". The bridge asks Fuseki's admin API for `dbType` `mem` or `tdb2` and Fuseki writes the file from its fixed template. The spec does not depend on the exact template text: it classifies by what the parsed file means, and treats anything it cannot place as the unsafe case. Cross-check: `create_dataset` records the kind it created; if the audit record and the parsed file disagree, treat the dataset as in-memory. Before the first real commit, run the classifier against the actual config files and compare the result with what you know about each dataset (see section 11).

**Where backups live (decision 6).** There are two different backup folders on the instance, and both are called `backups`. Keep them apart:

| Folder | Written by | Format | Retention |
|---|---|---|---|
| `$DATA_MOUNT/fuseki/base/backups` (`$BK` in the nightly script) | Fuseki's backup API, nightly | gzipped N-Quads, one file per dataset per run | copied to S3, then **local files deleted after 7 days** by `find "$BK" -type f -mtime +7 -delete` |
| `<holon-bridge>/backups` (`SNAPSHOT_DIR`, decision 6) | the bridge (`snapshot.py`) | Turtle, one file per named graph, plus `manifest.json` | newest `SNAPSHOT_KEEP` (default 5); the pinned default is never pruned |

`SNAPSHOT_DIR` must **not** be placed inside `$BK`: the nightly script's 7-day `find ... -delete` would remove the pinned default backup. A new setting `FUSEKI_BACKUP_DIR` (the `$BK` path) lets the bridge refuse to start with a `SNAPSHOT_DIR` inside it. `snapshot.py` lays its folder out as:

```
backups/{dataset}/{id}/manifest.json
backups/{dataset}/{id}/g0001.ttl ...      # id is the UTC time, e.g. 20261122T203109Z
```

and keeps the newest `SNAPSHOT_KEEP` (default 5).

**The default backup.** A dataset's *default* backup is a pinned copy in the same tree:

```
backups/{dataset}/default/manifest.json
backups/{dataset}/default/g0001.ttl ...
```

- It sits outside the id-named directories, so the existing pruning in `take()` (which removes only id-named directories) can never delete it, and `snapshot_ids()` does not list it.
- It is created only by an explicit call, a new admin tool `pin_default_backup(dataset, snapshot="latest")`, which copies that snapshot's directory to `default/`. Pinning replaces any earlier default.
- `snapshot.restore()` gains one case: `snapshot="default"` reads `backups/{dataset}/default/`. Every existing check stays (strict id patterns, same-dataset manifest, empty target).
- `restore_from: "auto"` (the default) uses the default backup if one exists, otherwise the newest of the bridge snapshots and the nightly dumps (below). `"latest"` forces that newest one; `"default"` forces the pinned copy and fails if none is pinned.

**Nightly dump as a restore source (decision 11, proposed).** The nightly script already writes every dataset, in-memory ones included, as a gzipped N-Quads file in `$BK` (named `{dataset}_{timestamp}.nq.gz` **[confirm the exact name]**). Those files can serve as a restore source as well as a disaster-recovery copy:

- The candidates for "latest" are the bridge snapshots and the newest nightly dump per dataset in `$BK`; the newest by time wins. The dry run's `backup_source.kind` says which: `"default"`, `"snapshot"` or `"nightly_dump"`.
- Restoring from a dump is a different path from `snapshot.restore()`: gunzip the file and load the quads into the empty dataset with one `POST` of `application/n-quads` to the dataset's own endpoint **[confirm]**. It keeps `snapshot.py`'s rules: the target must be empty (apart from the audit graph) and nothing is overwritten.
- There is no manifest to compare counts against, so verification counts the quads in the file and compares that with a `COUNT` over all graphs after the load. A mismatch is reported exactly as for a snapshot.
- A nightly dump is up to about 24 hours old, so `unsaved_changes` is `true` for it. The fresh pre-restart snapshot (`snapshot_first`) is newer, so it wins under `auto` unless a default is pinned.
- The dump is streamed, so it has its own cap, `NIGHTLY_RESTORE_MAX_BYTES` (compressed), and does not count against `SNAPSHOT_MAX_BYTES`.
- **S3 fallback (decision 13).** Order of preference under `auto`: pinned default, then the newest of the bridge snapshots and the dumps in `$BK` (7 days), then the newest dump in `s3://$BUCKET/backups/`, then the bridge snapshots synced to `s3://$BUCKET/bridge-backups/`. An S3 source is used only when nothing local exists for that dataset, and is downloaded to a temporary file, restored as above, then deleted. It needs `s3:GetObject` and `s3:ListBucket` on those two prefixes for the instance role **[confirm]**; the script's `sync` already needs the write side. The dry run reports `backup_source.kind` as `"s3_dump"` or `"s3_snapshot"`, with the key, and `unsaved_changes: true`. If S3 cannot be reached, the dataset counts as having no usable backup, which is a blocker unless named in `acknowledge_loss`.

**What `snapshot.py` forces on the design.**

- **An empty dataset is never snapshotted** (`nothing_to_snapshot`, 409). So an empty in-memory dataset is reported `empty: true`, skipped, and is not a blocker.
- **Restore fills only an empty dataset and never overwrites.** That fits: after a restart the dataset is empty, apart from the audit graph, which `restore` merges and does not replace. A restore that fails part-way removes the graphs it wrote, so it can simply be retried.
- **A snapshot is capped at `SNAPSHOT_MAX_BYTES`** (default 50 MB), read through the bridge's memory. A dataset over the cap cannot be snapshotted.
- **A snapshot holds every named graph**, including the shapes graph and the identity and ownership data, so a successful restore also restores shapes and owner. `seed_dataset_shapes` (reseed) is the fallback only where there is no snapshot.

**Order of operations on commit.**

1. Take the write lock; drain writes.
2. If `snapshot_first`, snapshot each non-empty in-memory dataset now. Writes are held, so the snapshot is exact. If a default backup is pinned and `restore_from` is `auto`, the default is still used for the restore, the fresh snapshot is kept alongside it, and the dry run reported `unsaved_changes: true` for the changes the default does not contain.
3. Pre-flight the config files; run the restart (section 6).
4. When Fuseki is back, restore each in-memory dataset from its chosen backup with `snapshot.restore`.
5. Verify: compare per-graph triple counts with the snapshot manifest. A mismatch sets that dataset's `restored: false` and `counts_match: false`; the snapshot is kept so `restore_dataset` can be retried by hand.
6. For an in-memory dataset restarted under `acknowledge_loss` (no backup), run `reseed_shapes` (never overwrites) so the dataset is at least usable, and report `changes_lost: true`.
7. Release the write lock; resume the scheduler.

## 5. Backup warning for in-memory datasets

An in-memory dataset is not in the Fuseki database files, so a backup that copies those files silently omits it. Every operation that *prepares a backup* therefore reports it.

**Shared helper.** `in_memory_datasets(conn)` (section 4.4 detection), used by every caller below.

| Operation | Behaviour |
|---|---|
| `snapshot_dataset` / `export_dataset` with no dataset named (all datasets) | Include a `warnings` entry; offer `include_in_memory: true`. |
| `snapshot_dataset` / `export_dataset` naming a dataset | Fine as before. The dataset is backed up explicitly. |
| `restart_fuseki` dry run | Reports them under `in_memory` (section 3.2). |
| The nightly `holonbridge-backup` script | Already covers in-memory datasets (decision 7). No warning is needed for the dump itself; see below. |

**Nightly backup (decision 7, revised after reading the script).** The unit files and the script are now known:

```
holonbridge-backup.timer     OnCalendar=*-*-* 03:15:00 UTC, RandomizedDelaySec=300, Persistent=true
holonbridge-backup.service   Type=oneshot, After=fuseki.service, ExecStart=/usr/local/sbin/holonbridge-backup
```

The script (`set -euo pipefail`):

1. Lists every dataset from `GET /$/datasets`, with no filtering by type.
2. For each one, `POST /$/backup/{ds}` (Fuseki's backup API), then polls `/$/tasks/{id}` for up to 1800 s. The whole run fails if one does not finish.
3. `aws s3 sync "$BK" "s3://$BUCKET/backups/"`, copies `config.json` to S3, and deletes local files in `$BK` older than 7 days.

Consequences:

- **In-memory datasets are already in the nightly backup**, as gzipped N-Quads. The pre-step proposed in draft 5 (`holonbridge.backup_prep`, the `runuser` drop-in and `BACKUP_INCLUDE_IN_MEMORY`) is **dropped**. Nothing is added to that unit for coverage.
- The backup holds only what existed at about 03:15 UTC. Changes to an in-memory dataset since then are "unsaved" at restart, which is what `snapshot_first` is for.
- **The bridge's own snapshots are not in the nightly backup**: the script syncs only `$BK`. A pinned default backup and any pre-restart snapshots live only on the instance's disk unless the script gets a second line, `aws s3 sync "$BRIDGE_DIR/backups" "s3://$BUCKET/bridge-backups/" --only-show-errors`, with `BRIDGE_DIR` added to `/etc/holonbridge/host.conf`. It has no `--delete`, so S3 keeps history. Accepted (decision 12). The bridge directory path is still to be confirmed on the instance (`BRIDGE_DIR`).
- The restart tool must not collide with the nightly run: see the "backup task is running" blocker in section 4.2. A restart during a dump would make the script's next `curl -fsS` fail and abort the run.

**Warning shape** (for interactive callers).

```json
{
  "warnings": [{
    "code": "in_memory_datasets_active",
    "datasets": ["scratch"],
    "covered_by_this_backup": false,
    "message": "1 in-memory dataset is active and is not in this backup. It is lost if Fuseki restarts.",
    "offer": {"include_in_memory": true}
  }]
}
```

## 6. Mechanism: how the bridge restarts a service it cannot sudo

Do not give the bridge `sudo` or a systemd D-Bus grant. Use a path unit so the bridge never holds the privilege:

```
# /etc/systemd/system/holonbridge-fuseki-restart.path
[Path]
PathExists=/var/lib/holonbridge/restart-fuseki.request
[Install]
WantedBy=multi-user.target

# /etc/systemd/system/holonbridge-fuseki-restart.service  (runs as root)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/holonbridge-restart-fuseki
```

`holonbridge-restart-fuseki` (root-owned, not writable by the bridge user):

1. Read the request file and delete it first, so a failure cannot re-trigger.
2. If the request says pre-flight passed, copy `FUSEKI_CONFIG_DIR/*.ttl` to `last-known-good/`. Otherwise leave the existing copy alone.
3. `systemctl restart fuseki.service`.
4. **Rollback (accepted, part of v1).** If `fuseki.service` is not active 60 s later, restore `last-known-good/*.ttl`, restart once more, and record `rolled_back: true`. A failed restart otherwise takes the whole instance down until someone logs in.
5. Write `restart-fuseki.result` (exit status, timestamps, `rolled_back`) for the bridge to read.

The bridge side writes the request, then polls `GET /$/ping` and `/$/datasets` until the datasets return or `wait_seconds` elapses. This needs no AWS API access, which matches the instance role's limits.

After a rollback the pending config change (for example the new `shacl` endpoint) is not applied. The result reports `rolled_back: true` and the same `pending_config` list, so the caller knows the change did not take effect. In-memory datasets are still restored after a rollback, since Fuseki has restarted either way.

## 7. Read-only companion: `get_backend_status`

```
get_backend_status() -> {store, reachable, datasets[], in_memory[], pending_config[], last_restart, restart_available}
```

`pending_config` compares each dataset's assembler file (endpoints declared) with the services Fuseki actually reports and lists differences such as "shacl declared, not served". Any authenticated user may call it.

## 8. `create_dataset` returns the restart plan, and can commit it (decisions 9 and 10)

Today `create_dataset` appends the shacl endpoint to the new dataset's config and reports `restartRequired`, leaving the caller to work out what a restart would do. Change `dataset_create.create()` so that, whenever `shacl_configured` is true (or the dataset is otherwise not yet serving `/shacl`), it also runs the **dry run** of section 3.2 through a shared, read-only `plan_restart()` and returns:

```json
{
  "dataset": "scratch",
  "kind": "memory",
  "restart": {
    "required": true,
    "reason": "shacl endpoint configured, served after the next Fuseki restart",
    "can_restart": true,
    "plan": { "...the section 3.2 body...": "" },
    "next": "restart_fuseki(commit=true, reason=...)"
  }
}
```

- `can_restart` is `true` only if the caller is on `FUSEKI_RESTARTERS`; otherwise the plan is still shown, so the creator can ask someone who is.
- The plan includes the new dataset itself under `in_memory` when it is a memory dataset (empty, so not a blocker).
- By default the call stays a dry run. Creating a dataset does not restart Fuseki unless the caller asks for it (below), because a restart interrupts every other dataset and user.
- The existing `notes` text for memory datasets ("call seed_dataset_shapes after a restart") is updated to say the restart tool restores or reseeds the dataset.

### 8.1 Opt-in commit (decision 10)

`create_dataset` gains two parameters:

```
restart:        "plan" | "commit" = "plan"
restart_reason: str   # used as the audit reason when restart = "commit"; defaults to "create_dataset {name}"
```

With `restart: "commit"`:

1. **Authorise before creating anything.** If the caller is not on `FUSEKI_RESTARTERS`, refuse the whole request with 403 *before* the dataset is created, so a refused commit never leaves a half-finished dataset behind. (With `"plan"` there is no such check.)
2. Create the dataset, add the shacl endpoint, seed the shapes and claim ownership, exactly as today.
3. Run `restart_fuseki(commit=true, snapshot_first=true, restore_from="auto")` with the caller's identity. Because the new dataset now holds its seeded shapes and ownership it is non-empty, so a new in-memory dataset is snapshotted before the restart and restored after it.
4. Return the creation result with the `restart` block carrying the section 3.3 commit result in place of the plan.

If the dry-run part finds blockers, the dataset is **still created** (it already exists by step 2), and the response reports `restart: {required: true, restarted: false, blockers: [...]}` so the caller can fix the blocker and call `restart_fuseki` directly. The restart is never retried automatically.

Because a commit restarts every dataset, `"commit"` is never the default and never inferred.

## 9. Fluree store, and the deferred decision

The bridge selects the store with `STORE_KIND` (`fuseki` or `fluree`). What I found in `holon-bridge-python`:

- **This tool and the in-memory rules are Fuseki-specific.** Fluree has no assembler files or per-dataset service endpoints. Ledgers appear through its own API without a restart, and `FlureeClient.list_datasets` reports a fixed `["data","query","update"]` for every ledger, so "pending config" detection cannot fire. Fluree has no SHACL endpoint either (its write gate is the read-only merge check in `fluree_shacl.py`), so the 405 problem cannot occur there.
- **Required behaviour:** on a Fluree store the tool returns `409 {"error":"not_applicable","store":"fluree"}` in dry run and commit alike, and `create_dataset` omits the `restart` block. Add a `restart(conn)` method to the `Store` protocol in `store.py`; `FusekiClient` implements it (section 6) and `FlureeClient` raises `NotApplicable`. The route then needs no `if store_kind` branches.
- **Deferred (decision 4): per-bank `store_kind`.** In the source I read, `STORE_KIND` is a single process-wide `Settings` value and `Bank` has no store kind of its own. A deployment with one Fuseki bank and one Fluree bank would need a per-bank `store_kind` or a second bridge process. **Revisit this before the first Fluree test**: the restart tool's store check should read the bank's kind, not the process's. **[confirm]** whether the deployed holonbridge.dev matches this source.

## 10. Audit

Write through `audit.py`: caller, `reason`, dry-run or commit, blockers, before and after dataset lists, per-dataset in-memory outcome (backup used, restored, counts match, changes lost), rollback, duration. Failed and refused calls are logged too. `pin_default_backup` writes an audit record as well.

## 11. Tests (extending `tests/`)

- Off by default: empty `FUSEKI_RESTARTERS` returns 403 for everyone, including admin.
- Refuses on: non-empty in-memory dataset with no usable backup, unparseable assembler file, write in flight, interval (60 s default) not elapsed, non-loopback bank, Fluree store.
- Classifier: `ja:MemoryDataset`, TDB2 with `"--mem--"`, TDB2 with a path, a `text:TextDataset` wrapping each, and an unknown or file-less dataset (treated as in-memory). **Run it once against the real config directory** and check each result by eye before the first commit.
- In-memory dataset with a snapshot: commit restores it and `counts_match` is true.
- `snapshot_first` makes a fresh snapshot and `changes_lost` is false; with a pinned default and `auto`, the default is used and `unsaved_changes` is reported.
- A pinned default survives pruning (take more than `SNAPSHOT_KEEP` snapshots) and is not listed as an id.
- An empty in-memory dataset is not snapshotted and is not a blocker.
- `acknowledge_loss` lets a backup-less dataset through, reseeds its shapes, and records the loss in the audit log.
- Backup operations with no dataset named emit `in_memory_datasets_active`; `include_in_memory` covers them.
- A Fuseki backup task still running (`/$/tasks`) is a blocker; a finished one is not.
- Restore from a nightly dump: gunzip and load into an empty in-memory dataset, quad count matches, `unsaved_changes` is `true`; a non-empty target is refused; a dump over `NIGHTLY_RESTORE_MAX_BYTES` is not used.
- Source choice: a pinned default beats everything under `auto`; otherwise the newest of snapshots and dumps wins, and a fresh `snapshot_first` snapshot is newer than any dump.
- Startup refuses a `SNAPSHOT_DIR` inside `FUSEKI_BACKUP_DIR`.
- `create_dataset` returns a `restart` block with `can_restart` correct for a restarter and a non-restarter, and does not restart by default.
- `create_dataset(restart="commit")`: a non-restarter gets 403 and **no dataset is created**; a restarter gets the dataset, a restart, and a restored in-memory dataset; with a blocker present the dataset exists, `restarted` is `false` and the blockers are listed.
- Dry run changes nothing (no request file written).
- Commit writes exactly one request file, then reports `services_gained` using a stubbed Fuseki that gains `shacl` on the second `/$/datasets` call.
- Rollback: a stubbed service that fails to start triggers the restore-and-retry path and `rolled_back: true`.
- Concurrent commits: the second gets 409.
- A test, as `test_dataset_create.py` does for drops, that no route restarts a store other than via the path-unit request file.

## 12. Still open

Decisions 11, 12 and 13 are accepted. What remains is detail that only the instance can answer:

1. **Dump file name** (section 4.4). Assumed `{dataset}_{timestamp}.nq.gz`. `ls "$DATA_MOUNT/fuseki/base/backups" | head` shows the real pattern, which the dump finder and the S3 listing both depend on.
2. **Bridge directory path** for `BRIDGE_DIR` in `/etc/holonbridge/host.conf` (section 5), and therefore the final `SNAPSHOT_DIR`.
3. **Instance role S3 read access** (section 4.4): `aws s3 ls "s3://$BUCKET/backups/" | tail -3` run as the bridge's user shows whether `ListBucket` and `GetObject` are already allowed. Writes are, since the sync works.
4. **N-Quads load path** (section 4.4): that `POST` of `application/n-quads` to the dataset endpoint accepts a gunzipped dump, including any RDF 1.2 syntax Fuseki wrote.
5. **Decision 4, per-bank `store_kind`** (section 9). TBD. Revisit before the first Fluree test. Nothing in this spec depends on it until then.
