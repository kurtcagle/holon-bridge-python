---
id: https://w3id.org/databook/holonbridge-personal-profile-plan-v1
title: "HolonBridge personal profile and executable: deployment plan"
type: databook
version: 0.1.0
created: 2026-11-07
author:
  - name: Kurt Cagle
    iri: https://holongraph.com/people/kurt-cagle
    role: orchestrator
  - name: Claude Sonnet 5.5
    role: transformer
description: >
  Plan for a single-user "personal" profile of holon-bridge-python (graph memory
  for an LLM agent in claude.ai chat or Claude Code, one Fuseki, many datasets),
  an opt-in automation group, and a runnable executable with an installer that
  sets the environment. Plan only: no code has been written. Grounded in the
  current code (server.py, deps.py, acl.py, envfile.py, config.py,
  service_config.py, dataset_create.py, .env.example) as of main at PR #78.
graph:
  namespace: "urn:holonbridge:plan:2026-11:"
  triple_count: 52
  subjects: 13
  rdf_version: "1.1"
  turtle_version: "1.1"
process:
  transformer: "Claude Sonnet 5.5"
  transformer_type: llm
  transformer_iri: https://api.anthropic.com/v1/models/claude-sonnet-5-5
  inputs:
    - iri: https://github.com/kurtcagle/holon-bridge-python/blob/main/holonbridge/server.py
      role: reference
    - iri: https://github.com/kurtcagle/holon-bridge-python/blob/main/holonbridge/deps.py
      role: reference
    - iri: https://github.com/kurtcagle/holon-bridge-python/blob/main/holonbridge/acl.py
      role: reference
    - iri: https://github.com/kurtcagle/holon-bridge-python/blob/main/holonbridge/envfile.py
      role: reference
    - iri: https://github.com/kurtcagle/holon-bridge-python/blob/main/holonbridge/service_config.py
      role: reference
    - iri: https://github.com/kurtcagle/holon-bridge-python/blob/main/.env.example
      role: reference
  timestamp: 2026-11-07T21:04:36Z
  agent:
    name: Claude Sonnet 5.5
    role: orchestrator
  note: >
    Written from a design conversation on 2026-11-07. Route and tool counts are
    taken from the decorators in the repository on that date and are estimates
    until the surface test in Phase A replaces them.
---

## Status

Plan only. No code has been written, and none is scheduled until a working
development machine is back. Nothing here blocks on that machine for long:
Phases A to C can be built and tested on Linux, and the Windows and macOS
builds come from CI (Phase D).

Phase B assumes PR #80 (zipped TriG export, signed download links, retention by
age) is merged, because the scheduled export job builds on it.

## Decisions so far

| # | Decision | Status |
|---|---|---|
| 1 | The personal edition is a **runtime profile** of the one codebase (`HOLONBRIDGE_PROFILE=personal`), not a fork | proposed |
| 2 | **Automation** (triggers, scheduler, projections, named projections) is **opt-in**, off by default (`HOLONBRIDGE_FEATURES=automation`) | decided |
| 3 | **Jena Fuseki is a separate, local install** on the same machine as the bridge. It is not bundled or supervised by the executable | decided |
| 4 | An **installer / `init` step sets the environment** the bridge and MCP server need | decided; mechanism proposed below |
| 5 | The personal edition is released under **Apache-2.0**. The multi-user edition is out of scope for this plan and carries separate terms | decided |
| 6 | Audit stays on at "mutations" level, and destructive graph operations are snapshotted first | proposed |
| 7 | Dockerfile / container image | deferred |
| 8 | Splitting the multi-user authorisation code behind an interface (Phase E) | deferred |

## What the profile exposes

Counts come from route and tool decorators on 2026-11-07 and are approximate.
The surface test (Phase A) fixes the exact sets.

| Group | Contents | Routes | MCP tools |
|---|---|---|---|
| **Core** (personal) | SPARQL, graph push/get/drop/list, `get_holon`, validate, named queries and rules, ingest and pipelines, datasets (create, list, switch, snapshot, export), sequences, fluents, events, audit (read), dataset admin | about 59 | about 52 |
| **Automation** (opt-in) | triggers, candidates, scheduler, projections, named projections | about 39 | about 36 |
| **Multi-user** (not in the personal profile) | identity admin, delegation, personas, `whoami`, dataset claim, agents, banks | about 34 | about 24 |

The full build exposes all three groups: about 132 routes and 112 tools. The
personal profile drops about 40 per cent of the routes and more than half of
the tool definitions Claude has to carry in context.

## Personal profile design

### Identity

- One identity-bound token (`IDENTITY_TOKENS`) serves both REST clients and the
  MCP server. The credential, not a header, decides who the caller is.
- `ALLOW_LEGACY_BEARER=false`, so the shared-secret path that lets a caller
  name any identity is off. Startup already refuses that setting when no
  identity or proxy token exists, which this configuration satisfies.
- The owner is named in `DATASET_CLAIMANTS` and `DATASET_CREATORS`, and every
  new dataset is claimed to the owner automatically, without the in-chat
  confirmation step the multi-user build requires.
- For the claude.ai chat connector, the GitHub OAuth path stays and is pinned
  to a single login (`MCP_ALLOWED_GITHUB_LOGINS`). Claude Code over stdio or
  loopback needs no further authentication.

### The ACL stays in the process, inactive

`require_animus` is a dependency on every read and write route, and
`authorize_query` and `authorize_update` parse SPARQL to find the graphs it
touches. Every `check_*` function begins with an admin-role bypass. With the
owner claimed as admin on every dataset, the existing code runs and always
answers "admin role bypass". That costs one extra `ASK` per request, needs no
change to `acl.py`, and keeps the existing tests valid.

The multi-user modules (`acl.py`, `claim.py`, `acting_as.py`, `delegation.py`,
and the agents package) are therefore **inactive, not absent**, in Phase A.
Making them absent is the larger change in Phase E.

### Other behaviour

- One backend connection: the `local` bank is pinned, and `?bank=` is
  rejected. Multiple datasets still work through the dataset override.
- Automation off: its routers and tools are not registered,
  `SCHEDULER_ENABLED=false`, and the post-write trigger evaluation is skipped.
  Two things must be verified first: where the trigger hook is called on the
  write path, and whether any core module (named rules, pipelines) imports
  from the automation modules.
- SHACL validation on write stays available and defaults to off.
- **Safety replaces permissions.** With one user and an agent holding the owner
  token, the realistic risks are a bad write by the agent and a prompt injected
  through ingested content, not another user. So destructive operations
  (replace and drop of a graph) take an automatic snapshot first, and audit
  records what changed. The bridge has no dataset drop, which stays a
  deliberate manual step in Fuseki.
- A **surface test** asserts the exact route set and tool set for each profile,
  so any drift is a failing test.

## Executable and installer

### What ships

The frozen bridge and MCP server only. Jena is not bundled, so there is no Java
runtime to trim and no Fuseki jar to redistribute. Size is likely tens of
megabytes up to about 150 MB; this is unmeasured.

### Co-location with Jena is load-bearing

Fuseki's admin API creates a dataset only from a fixed template. To give a
dataset a `/shacl` endpoint, the bridge appends to the configuration file
Fuseki wrote (`{FUSEKI_CONFIG_DIR}/{name}.ttl`), and the endpoint appears only
after Fuseki is restarted, which the bridge never does itself. The same
mechanism provisions the text index.

So the bridge and Fuseki must share a filesystem, and the configuration
directory must be writable by the user running the bridge. A remote Fuseki
works for everything except those two provisioning steps. This is why "Jena
installed locally relative to the MCP machine" is a requirement and not just a
convenience.

`init` therefore creates the owner's first datasets, asks for one Fuseki
restart, and `doctor` then confirms the `/shacl` endpoint is live.

### Subcommands

| Subcommand | Purpose |
|---|---|
| `init` | Create the data directory, generate secrets, write `.env`, locate and verify Fuseki, create the first datasets, print or apply client configuration |
| `serve` | Run the bridge (the long-lived daemon) |
| `mcp` | The stdio MCP server; a thin client of a running `serve`, which it starts behind a lock file if absent |
| `doctor` | Check Java version, Fuseki reachability and version, writable configuration directory, `/shacl` endpoint, token, ports, and data directory permissions |
| `snapshot`, `export` | Wrap the existing snapshot and export operations |

Read the Java version Jena requires from the installed Jena release instead of
hard-coding it in `doctor`.

### Process model

`serve` is long-lived and holds the connection to Fuseki. `mcp` is short-lived
and spawned by Claude Code or Claude Desktop per session. Keeping `mcp` thin
makes its start-up fast, and the claude.ai remote transport and the local
clients share the same running bridge.

The MCP stdio transport breaks if anything other than protocol messages reaches
stdout. All logging from every subcommand goes to a file or stderr.

### Data directory and environment

- Per-user data directory `~/.holonbridge/` (the location `config.json` already
  uses): `.env`, `config.json`, snapshots and exports, logs, lock file.
- `envfile.py` today discovers `HOLONBRIDGE_ENV_FILE`, else `.env` in the
  current directory. An installed executable has no useful current directory,
  so add a third step: `~/.holonbridge/.env`. A real environment variable still
  wins over the file, as now.
- **Do not set machine-wide environment variables.** On Windows they need
  administrator rights, `setx` stores values in plain text in the registry,
  already-open shells do not see them, and every process of the user inherits
  secrets. The `.env` file carries everything, with owner-only permissions.

| Variable | Set by `init` to |
|---|---|
| `HOLONBRIDGE_HOST`, `HOLONBRIDGE_PORT` | `127.0.0.1` and a free port (default 3031) |
| `FUSEKI_URL` | confirmed against `/$/ping` (default `http://localhost:3030`) |
| `FUSEKI_CONFIG_DIR` | detected from the Jena install and confirmed |
| `IDENTITY_TOKENS` | one generated token bound to the owner identity |
| `ALLOW_LEGACY_BEARER` | `false` |
| `DATASET_CLAIMANTS`, `DATASET_CREATORS` | the owner identity |
| `HOLONBRIDGE_URL`, `HOLONBRIDGE_MCP_TOKEN` | the bridge address and the same token, for the MCP side |
| `HOLONBRIDGE_DATASET` | the default dataset |
| `SNAPSHOT_DIR`, `SNAPSHOT_KEEP`, `SNAPSHOT_MAX_AGE_DAYS` | data directory path, and retention defaults |
| `EXPORT_SIGNING_KEY` | a generated key of 32 or more characters |
| `EXPORT_MAX_BYTES` | default |
| `SCHEDULER_ENABLED` | `false` |
| `ANTHROPIC_API_KEY` | optional, prompted for (needed only by `nl_query`) |
| `MCP_PUBLIC_URL`, `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `MCP_JWT_SECRET`, `MCP_ALLOWED_GITHUB_LOGINS` | only when the claude.ai remote connector is wanted |

### Installer forms

The operating-system installers are thin wrappers that place the binary and put
it on the path, then run `init`. All the real work stays in one code path.

- **Windows:** a per-user installer (Inno Setup or similar) into the user's
  local programs folder, no administrator rights.
- **macOS:** a Homebrew tap first; a signed package later if needed.
- **Linux:** tarball plus install script; `.deb` and `.rpm` later; optional
  `systemd --user` unit.
- **Developer route:** `pipx install` or `uv tool install`, then `init`. The
  `holonbridge` and `holonbridge-mcp` entry points already exist.

### Client wiring

- **Claude Code:** `init` registers the stdio server with `claude mcp add`.
- **Claude Desktop:** `init` patches the configuration file, with a backup and
  only after confirmation.
- **claude.ai chat:** needs the remote transport on a public HTTPS URL, which
  means a tunnel (Cloudflare Tunnel, ngrok or Tailscale Funnel). `init` prints
  the steps; the tunnel itself is not bundled.

## Build and release

- GitHub Actions matrix: Windows, macOS (arm64 and Intel) and Linux. Build the
  Linux binary on an older distribution so it runs on more of them.
- Freeze with PyInstaller (fastest to get working) or Nuitka (better start-up,
  fewer antivirus false positives). Use a folder layout, not a single
  self-extracting file. Take care with rdflib's entry-point plugins, uvicorn's
  dynamic imports, pyshacl and the pinned `mcp` package.
- Each archive carries `LICENSE`, `NOTICE` and third-party licence texts, plus
  checksums. Jena is not redistributed.
- Signing: unsigned is acceptable for the author's own use. Anyone else needs
  Developer ID signing and notarisation on macOS and a code-signing certificate
  on Windows, or the operating system and antivirus will warn.

## Spikes, in order

1. Freeze the app and run the existing smoke tests against the frozen binary.
2. Add the data-directory step to `envfile.py` and test it from the Claude Code
   and Claude Desktop launchers.
3. Co-location: `doctor` finds Fuseki's base and configuration directories on
   each operating system; dataset creation, the appended `/shacl` endpoint and
   the one restart work end to end; Fuseki as a service versus a console
   process.
4. One export against a live Fuseki, including RDF 1.2 triple terms (still open
   from PR #80).
5. MCP stdio cleanliness and cold-start time under Claude Code.
6. Daemon lifecycle: lock file, auto-start by `mcp`, port collisions, detaching
   without a console window on Windows.

## Phases

| Phase | Scope | Size |
|---|---|---|
| A | Profile flag, shorter router and tool lists, automation gate, owner token and auto-claim, surface test | small |
| B | Snapshot before destructive operations, scheduled export to a chosen folder, `.env.personal.example`, `pipx` documentation | small |
| C | Spikes 1 to 6, then `init`, `serve`, `mcp`, `doctor` | medium |
| D | Installers, CI matrix, release archives, signing if needed | medium |
| E (deferred) | Replace the in-process ACL with an interface and a built-in owner implementation, so the multi-user code can be absent from the personal build | large |

Phase B includes the scheduled export because backups are the reason this work
started: do not sync a live TDB2 folder through a cloud drive (it can corrupt),
sync the export zips instead.

## Open decisions

- Operating system and CPU of the replacement development machine, which sets
  the first target to test natively.
- Whether anyone other than the author receives binaries, which sets whether
  signing is needed.
- Whether a container image is wanted for cloud hosting, or the local install
  is the only supported route.
- Whether audit read routes stay in the personal profile (proposed: yes).

## Plan as data

<!-- databook:id: plan-data -->
```turtle
@prefix plan:    <urn:holonbridge:plan:2026-11:> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .

plan:phase-a a plan:Phase ;
    rdfs:label "A. Profile and surface" ;
    plan:order 1 ;
    plan:size "small" .

plan:phase-b a plan:Phase ;
    rdfs:label "B. Safety and backups" ;
    plan:order 2 ;
    plan:size "small" ;
    plan:dependsOn plan:phase-a .

plan:phase-c a plan:Phase ;
    rdfs:label "C. Executable" ;
    plan:order 3 ;
    plan:size "medium" ;
    plan:dependsOn plan:phase-a .

plan:phase-d a plan:Phase ;
    rdfs:label "D. Installers and release" ;
    plan:order 4 ;
    plan:size "medium" ;
    plan:dependsOn plan:phase-c .

plan:phase-e a plan:Phase ;
    rdfs:label "E. Authorisation interface" ;
    plan:order 5 ;
    plan:size "large" ;
    plan:status "deferred" ;
    plan:dependsOn plan:phase-a .

plan:decision-automation-opt-in a plan:Decision ;
    rdfs:label "Automation is opt-in and off by default" ;
    plan:status "decided" ;
    dcterms:date "2026-11-07"^^xsd:date .

plan:decision-jena-external a plan:Decision ;
    rdfs:label "Fuseki is a separate local install, not bundled" ;
    plan:status "decided" ;
    dcterms:date "2026-11-07"^^xsd:date .

plan:decision-installer-sets-env a plan:Decision ;
    rdfs:label "An installer or init step sets the environment, through a per-user .env" ;
    plan:status "decided" ;
    dcterms:date "2026-11-07"^^xsd:date .

plan:decision-runtime-profile a plan:Decision ;
    rdfs:label "Personal edition is a runtime profile of one codebase" ;
    plan:status "proposed" .

plan:decision-audit-and-snapshots a plan:Decision ;
    rdfs:label "Audit at mutations level; snapshot before destructive operations" ;
    plan:status "proposed" .

plan:decision-target-os a plan:Decision ;
    rdfs:label "Operating system of the replacement development machine" ;
    plan:status "open" .

plan:decision-signing a plan:Decision ;
    rdfs:label "Whether binaries go to anyone else, and so whether to sign" ;
    plan:status "open" .

plan:decision-container a plan:Decision ;
    rdfs:label "Container image for cloud hosting" ;
    plan:status "deferred" .
```
