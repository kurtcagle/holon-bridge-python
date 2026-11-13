---
id: https://w3id.org/databook/holon-bridge-python-setup-v1
title: "holon-bridge-python — Download, Build, and Configure"
type: databook
version: 1.0.0
created: 2026-08-31
author:
  - name: Kurt Cagle
    iri: https://holongraph.com/people/kurt-cagle
    role: orchestrator
  - name: Chloe Shannon
    iri: https://holongraph.com/people/chloe-shannon
    role: transformer
domain: https://w3id.org/holon/
subject:
  - holon-bridge-python
  - HolonBridge setup
  - HGA Server
description: >
  Setup DataBook for kurtcagle/holon-bridge-python — prerequisites,
  download, build, and configuration (banks, .env, first-identity
  bootstrap, MCP transports). Scoped to getting a working instance
  running; architectural rationale and the full endpoint/design-notes
  reference live in the repo's own README.md.
process:
  transformer: "Claude Sonnet 5"
  transformer_type: llm
  transformer_iri: https://api.anthropic.com/v1/models/claude-sonnet-5
  inputs:
    - iri: https://github.com/kurtcagle/holon-bridge-python
      role: primary
      description: "pyproject.toml, .env.example, examples/config.json, holonbridge/bootstrap.py, holonbridge/conn.py, scripts/build-holonbridge-*, scripts/start-holonbridge.ps1 — read directly, not from memory"
  timestamp: 2026-08-31T18:00:00Z
  agent:
    name: Chloe Shannon
    iri: https://holongraph.com/people/chloe-shannon
    role: orchestrator
---

## Overview

`holon-bridge-python` is the Python HGA Server — the bridge between the
Holon Graph Architecture pipeline and an Apache Jena Fuseki backend, with
a FastAPI REST surface and a stdio/remote MCP layer on top. This DataBook
covers three things only: getting the code, building it, and configuring
it to run. Architectural rationale, the full endpoint table, and the
design-notes discussion of every write-mode/SHACL/scheduler subtlety live
in the repo's own `README.md` — this document exists so setup doesn't
require reading that whole file first.

> **Note:** `README.md`'s own "Profiles" section predates a rename — the
> code, `.env.example`, and `examples/config.json` all now say **bank**,
> not profile (`switch_bank`, `HOLONBRIDGE_BANK`, `BankStore`). This
> DataBook uses the current terminology throughout.

---

## Prerequisites

- **Python ≥ 3.10** (`pyproject.toml` — `requires-python = ">=3.10"`)
- **git**
- **Apache Jena Fuseki** — a real instance, already installed. Nothing in
  this repo installs or configures Fuseki itself.
- **ngrok** — only if exposing the MCP remote transport to claude.ai
  (`MCP_INBOUND_TOKEN` / GitHub OAuth path, below)

---

## 1. Download

```bash
git clone https://github.com/kurtcagle/holon-bridge-python.git
cd holon-bridge-python
```

Or let the build script do the clone (see §2) — both platform scripts
clone fresh or fast-forward-update an existing checkout, and refuse to
touch a target directory that exists but isn't a git checkout.

---

## 2. Build

### Automated (recommended)

Both scripts are re-runnable: existing clone updates in place, existing
venv and `.env` are left untouched unless you pass a reset flag.

**Ubuntu/Debian** — `scripts/build-holonbridge-ubuntu.sh`:

```bash
./scripts/build-holonbridge-ubuntu.sh
# or, to override defaults:
INSTALL_DIR=/opt/holon-bridge-python BRANCH=main \
  HOLONBRIDGE_EXTRAS=mcp,shacl,dev ./scripts/build-holonbridge-ubuntu.sh
```

Installs system prerequisites via `apt-get` (git, python3-venv/dev,
build-essential), clones/updates to `$INSTALL_DIR` (default
`/opt/holon-bridge-python`), creates `.venv`, installs the package
constrained by `constraints.txt`, and seeds `.env` from `.env.example`
with a freshly generated `BEARER_TOKEN`.

**Windows 11 / PowerShell** — `scripts/build-holonbridge-windows.ps1`:

```powershell
.\scripts\build-holonbridge-windows.ps1
# or:
.\scripts\build-holonbridge-windows.ps1 -InstallDir "D:\holon-bridge-python" -Branch main
```

Same shape: installs git/Python via winget if missing (refuses to
silently upgrade an existing-but-too-old Python — re-run with
`-PythonExe` pointed at a newer install instead), clones/updates to
`-InstallDir` (default `C:\ProgramData\jena-bridge-python`), builds
`.venv`, installs, seeds `.env` with a generated `BEARER_TOKEN`.

Both scripts default the `shacl` extra **on** (`mcp,shacl,dev`) — the
main README's own quickstart example (`.[mcp,dev]`) omits it, but
`SHACL_REQUIRED`/`SHACL_DELTA` are live on real deployments, so the
scripts install what's actually needed rather than the minimal example.

Neither script touches Fuseki or ngrok config — both stay explicitly
manual (see §3, §4).

### Manual

```bash
python -m venv .venv
source .venv/bin/activate          # PowerShell: .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -c constraints.txt -e ".[mcp,shacl,dev]"
```

Extras, from `pyproject.toml`:

| Extra | Adds | Needed for |
|---|---|---|
| `mcp` | `mcp>=1.2,<2` | the MCP layer (`holonbridge_mcp`) — upper-bounded deliberately, since `mcp` 2.0 removed `mcp.server.fastmcp` outright |
| `shacl` | `pyshacl>=0.26` | the SHACL validation gate |
| `dev` | `pytest`, `pytest-asyncio`, `ruff` | running the test suite |

Console scripts this installs (`pyproject.toml` → `[project.scripts]`):

| Script | Entry point | Purpose |
|---|---|---|
| `holonbridge` | `holonbridge.server:main` | the REST bridge |
| `holonbridge-mcp` | `holonbridge_mcp.__main__:main` | the MCP layer |
| `holonbridge-bootstrap-admin` | `holonbridge.bootstrap:main` | mint the first identity (§3.3) |

Verify: `.venv/bin/holonbridge --help` (or `.venv\Scripts\holonbridge.exe`
on Windows) should run without error.

---

## 3. Configure

Three things, roughly in order: `.env`, banks, and — only on a genuinely
fresh dataset — bootstrapping the first identity.

### 3.1 `.env`

```bash
cp .env.example .env
```

One file, loaded automatically by both `holonbridge` and
`holonbridge_mcp` (`holonbridge/envfile.py`) — a real environment
variable always wins over the file. Discovery is `HOLONBRIDGE_ENV_FILE`
(an explicit path — needed when the process is launched somewhere that
isn't the project directory, e.g. Claude Desktop's own config) falling
back to `.env` in the current working directory.

Core variables — the ones every setup needs:

| Variable | Default | Meaning |
|---|---|---|
| `HOLONBRIDGE_HOST` / `HOLONBRIDGE_PORT` | `127.0.0.1` / `3031` | where the REST bridge listens |
| `FUSEKI_URL` | `http://localhost:3030` | backend Fuseki |
| `FUSEKI_DATASET` | `ds` | the `local` bank's default dataset |
| `BEARER_TOKEN` | *(empty — set this)* | protects the REST bridge; generate with `openssl rand -hex 32` or the PowerShell RNG one-liner in `.env.example` |
| `PARSE_MODE` | `passthrough` | Jena parses Turtle (required for RDF 1.2/Turtle 1.2); `local` uses rdflib pre-parsing, Turtle 1.1 only |
| `SHACL_REQUIRED` | `false` | validate every push against `urn:{dataset}:shacl` |
| `SHACL_DELTA` | `true` | report only violations the payload introduces, not the whole merged graph |
| `FLUENT_GATE_SCOPE` | `assertion` | what the fluent update's pre-commit SHACL gate validates: the fluent's own assertion, or (`holder`) also the holder, its sibling fluents and one hop from the new value |
| `ALLOW_DATASET_OVERRIDE` | `true` | whether `X-Dataset-Override` is honoured at all |
| `HOLONBRIDGE_CONFIG` | `~/.holonbridge/config.json` | path to the bank config file (§3.2) |

Optional, only if used:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | LLM proposer for scheduler `LLMInvocation` tasks, and `nl_query` |
| `SCHEDULER_ENABLED` / `SCHEDULER_DATASET` / `SCHEDULER_TICK_SECONDS` / `SCHEDULER_MAX_FIRING_DEPTH` | off by default — enabling is deliberate, since it writes on its own initiative |
| `HOLONBRIDGE_URL`, `HOLONBRIDGE_DATASET`, `HOLONBRIDGE_BANK` | MCP-layer pins to a REST URL / dataset override / bank override |
| `MCP_INBOUND_TOKEN` | static bearer for the remote (SSE) MCP transport — independent of `BEARER_TOKEN`, which only protects the REST port |
| `GITHUB_OAUTH_CLIENT_ID` / `_SECRET`, `MCP_PUBLIC_URL`, `MCP_JWT_SECRET`, `MCP_ALLOWED_GITHUB_LOGINS` | GitHub OAuth as a second remote-MCP credential kind — setting `GITHUB_OAUTH_CLIENT_ID` makes the rest of this group required |
| `NGROK_AUTHTOKEN` | read only by `scripts/holonbridge-ctl.sh`, not by either Python process |

Full detail on the remote-transport and GitHub OAuth variables — including
the DNS-rebinding allowlist and OAuth App registration steps — is in the
main README under "Remote transport, for claude.ai" and "GitHub OAuth, as
a second credential kind"; out of scope here beyond the table above.

### 3.2 Banks

A **bank** (formerly "profile") is a named backend connection: server URL,
default dataset, optional bearer auth. Configured in
`~/.holonbridge/config.json` (or wherever `HOLONBRIDGE_CONFIG` points), a
`local` bank is always synthesised from `.env` if the file doesn't exist,
so the bridge starts cleanly with no config file at all.

```json
{
  "default": "local",
  "banks": {
    "local": {
      "url": "http://localhost:3030",
      "dataset": "ds",
      "auth": null
    },
    "bridgerton": {
      "url": "http://localhost:3030",
      "dataset": "bridgerton",
      "auth": null
    },
    "remote-example": {
      "url": "https://example.org",
      "dataset": "example",
      "auth": { "type": "bearer", "token": "REPLACE_ME" }
    }
  }
}
```

(`examples/config.json` in the repo — copy and edit.)

### 3.3 Bootstrap the first identity

**Only needed on a genuinely fresh dataset — one with no `holon:Person`
minted yet, not even your own.** Every identity-gated route requires an
already-resolved Person for the caller before it will do anything, which
means there is no route through the REST API that can create that very
first Person — the gate that would authorise the write is the thing not
yet satisfied. `holonbridge-bootstrap-admin` talks to Fuseki directly,
bypassing the gate entirely, specifically to break that circularity.

```bash
holonbridge-bootstrap-admin \
  --slug kurt \
  --name "Kurt Cagle" \
  --github-user kurtcagle \
  --role admin \
  --bank local
```

Idempotent — checks by external identity before writing anything, so
it's safe to leave in a startup script unconditionally; a no-op once the
identity exists. It never touches an existing identity's role on a
re-run, so a later promotion or demotion elsewhere isn't clobbered.
Everyone after the first person should go through the ordinary admin
route (`POST /admin/person`, now unblocked) — this command is the one
deliberate exception to that path, not a replacement for it.

What it writes (illustrative — the actual Turtle this command generates
for the example above):

```turtle
PREFIX holon: <https://w3id.org/holon/>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

<urn:ds:person:kurt> a holon:Person ;
    rdfs:label "Kurt Cagle" ;
    holon:hasExternalIdentity <urn:ds:person:kurt:identity:github> ;
    holon:hasRole <urn:ds:role:admin> .

<urn:ds:person:kurt:identity:github> a holon:GitHubIdentity ;
    holon:identifier "kurtcagle" .

<urn:ds:role:admin> a holon:Role ;
    rdfs:label "Admin" .
```

`start-holonbridge.ps1` (§4) can call this automatically on every launch
via `-BootstrapAdminGithubUser`/`-BootstrapAdminSlug`/`-BootstrapAdminName`
— unset by default, so nothing changes unless you opt in.

---

## 4. Run

**Fuseki must already be up** before any of the below — none of this
starts, installs, or configures it.

### REST bridge alone

```bash
holonbridge          # listening on the HOLONBRIDGE_HOST:HOLONBRIDGE_PORT from .env
```

### MCP — stdio (Claude Desktop / Claude Code)

```json
{
  "mcpServers": {
    "holonbridge-py": {
      "command": "/path/to/holon-bridge-python/.venv/bin/python",
      "args": ["-m", "holonbridge_mcp.server"],
      "env": { "HOLONBRIDGE_ENV_FILE": "/path/to/holon-bridge-python/.env" }
    }
  }
}
```

`HOLONBRIDGE_ENV_FILE` matters here specifically because Claude Desktop
doesn't launch from the project directory, so the implicit
`.env`-in-CWD discovery a shell gives you for free doesn't apply.

### MCP — remote (claude.ai, via ngrok)

```bash
export MCP_INBOUND_TOKEN=$(openssl rand -hex 32)
python -m holonbridge_mcp --transport sse --port 3032
ngrok http --url=your-subdomain.ngrok.io 3032
```

Add the ngrok URL plus `/sse` as a custom connector in claude.ai, with the
token as an `Authorization: Bearer` header. The remote transport refuses
to start with no credential configured at all (neither
`MCP_INBOUND_TOKEN` nor GitHub OAuth) — deliberately, since an
unauthenticated caller on a tunnelled endpoint would inherit
`sparql_update`/`push_turtle` with nothing standing in the way.

### All three together

Both control scripts start Fuseki-adjacent orchestration, the REST
bridge, the MCP server, and its ngrok tunnel as one unit, with output to
timestamped log files and a clean teardown on interrupt:

```powershell
.\scripts\start-holonbridge.ps1
```

```bash
./scripts/holonbridge-ctl.sh start [--with-mcp]
./scripts/holonbridge-ctl.sh status
```

`start-holonbridge.ps1` sets `MCP_PUBLIC_URL` in-process from the same
`-McpNgrokUrl` value that starts the tunnel, rather than trusting a
separately-maintained `.env` to agree with it — a mismatch there is the
most likely explanation for a GitHub OAuth window that opens and never
resolves.

---

## 5. Verify

```bash
export HB=http://localhost:3031
export HBTOK=<your-bearer-token>

curl -s "$HB/health"
curl -s -H "Authorization: Bearer $HBTOK" "$HB/endpoint"
curl -s -H "Authorization: Bearer $HBTOK" "$HB/graphs"
```

`/health` needs no token. `/endpoint` should report the active bank,
dataset, and resolved graph IRIs; `/graphs` should list named graphs with
triple counts (empty is fine on a genuinely fresh dataset).

Run the test suite (stubbed at the `FusekiClient` boundary — no live
Fuseki needed):

```bash
python -m pytest -q
```

---

## Known limitation, worth knowing before relying on `write_mode=Replace`

Named-rule execution under `write_mode=Replace` has an open, unresolved
issue — it can silently drop a triple while reporting success (tracked as
[issue #1](https://github.com/kurtcagle/holon-bridge-python/issues/1)).
Not a configuration problem and nothing here works around it; flagged
here only so a fresh setup doesn't discover it the hard way. `Sync` mode
over the same input is unaffected.
