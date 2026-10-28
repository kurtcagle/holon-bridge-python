"""Dataset listing and override tools.

Both tools here read or write the calling identity's own dataset override
via ``session.current_dataset_override()`` / ``session.set_dataset_override()``
-- never the raw ``session._dataset_overrides`` dict directly -- so this
module imports ``session`` itself (qualified access), not just
``mcp``/``_call``. See ``session.py``'s own docstring for why a plain name
import would go stale, and for the 2026-09-23 change from one override per
process to one per caller identity.

CHANGED 2026-09-27: added ``provision_text_index``. Thin wrapper over
``POST /datasets/{name}/text-index`` -- all the actual patch logic lives
server-side in ``provisioning.py``; this module just forwards the call.
Admin-gated on the bridge side, same as ``act_as``.

CHANGED 2026-10-27: added ``create_dataset``. Create only -- there is no drop
tool, deliberately. Gated server-side by ``DATASET_CREATORS``, not by admin on
any one dataset.

CHANGED 2026-10-27: added ``seed_dataset_shapes``, for a dataset that came back
empty (an in-memory one after a Fuseki restart). Same gate as ``create_dataset``.
"""

from __future__ import annotations

from .. import session
from ..session import mcp, _call


@mcp.tool()
async def list_datasets() -> dict:
    """List the datasets the backend actually hosts.

    Different question from ``list_banks``, which reports the bridge's
    configured banks. A bank names a dataset it expects; this says what is
    really there. ``active`` marks the one *your own* calls are currently
    going to -- since 2026-09-23 this is your own dataset override, not a
    single value every caller through this process shares; a colleague
    calling this at the same moment can correctly see a different
    ``active`` if they've switched to a different dataset themselves.
    """
    result = await _call("GET", "/datasets")
    if isinstance(result, dict) and not result.get("error"):
        result["active"] = session.current_dataset_override() or result.get("active")
        result["datasetOverrideSource"] = session.current_dataset_override_source()
    return result


@mcp.tool()
async def switch_dataset(name: str) -> dict:
    """Point every subsequent call *you* make at a different dataset.

    Validates first: switching to a name the server does not host would
    otherwise produce empty reads that look exactly like empty data, which is
    a slow way to find a typo. Sets ``X-Dataset-Override`` on every following
    request you make, so it needs no restart and no second bridge process.

    Scoped to your own identity, not to this process as a whole -- since
    2026-09-23, a colleague reaching the same running holonbridge-mcp
    process under their own identity is completely unaffected by this call,
    and this call is unaffected by theirs. Before that change this override
    was a single value shared by every caller through the same process,
    which meant one person's switch silently moved the ground under
    everyone else's very next call.

    Persisted to disk under your own identity, not held only in memory — a
    process restart restores your choice instead of silently falling back
    to whatever ``HOLONBRIDGE_DATASET`` (or nothing) says, which is exactly
    the failure that motivated this. An explicit ``HOLONBRIDGE_DATASET`` env
    var still wins over any persisted value, for every identity alike, on
    the *next* restart, the same as every other setting in this codebase —
    persistence is a convenience for the ordinary case, not a way to
    override a deliberate operator-level pin.

    Pass an empty string to clear your own override and fall back to the
    active bank's own dataset. Clearing also removes your persisted file, so
    a restart after clearing starts genuinely fresh rather than restoring
    the just-cleared value.
    """
    if not name.strip():
        session.set_dataset_override("", "none")
        endpoint = await _call("GET", "/endpoint")
        return {
            "ok": True,
            "cleared": True,
            "dataset": endpoint.get("dataset") if isinstance(endpoint, dict) else None,
            "note": "override cleared; using the active bank's dataset",
        }

    check = await _call("GET", f"/datasets/{name.strip()}")
    if isinstance(check, dict) and check.get("error"):
        return {
            "ok": False,
            "requested": name,
            "detail": check.get("detail"),
            "note": "dataset not switched",
        }

    session.set_dataset_override(name.strip(), "explicit")
    return {
        "ok": True,
        "dataset": name.strip(),
        "graphs": check.get("graphs") if isinstance(check, dict) else None,
    }


@mcp.tool()
async def provision_text_index(
    dataset: str,
    extra_predicates: list[dict] | None = None,
    lucene_dir: str | None = None,
    commit: bool = False,
) -> dict:
    """Preview or apply a Lucene full-text index patch to a dataset's Fuseki
    assembler file.

    Admin-only on the bridge side (same gate as ``act_as``). Defaults to a
    dry run (``commit=false``) that returns the full proposed file content
    without writing anything -- read it over, then call again with
    ``commit=true`` once it looks right. Idempotent: re-running this against
    an already-provisioned dataset reports ``changed: false`` rather than
    adding a second index. Always requires a Fuseki restart to take effect;
    this tool never restarts Fuseki itself.

    Default indexed fields are the standard label/description vocabulary --
    rdfs:label, rdfs:comment, skos:prefLabel/altLabel/definition/comment,
    dcterms:title, sh:name, sh:description. ``extra_predicates`` adds fields
    specific to this dataset's own schema, each as
    ``{"field": "...", "prefix": "...", "local": "..."}``, e.g.
    ``{"field": "noteText", "prefix": "chloe", "local": "noteText"}``.

    Requires ``FUSEKI_CONFIG_DIR`` to be set on the bridge process -- this
    only works when the bridge and Fuseki share a filesystem.
    """
    return await _call(
        "POST",
        f"/datasets/{dataset}/text-index",
        json_body={
            "extra_predicates": extra_predicates,
            "lucene_dir": lucene_dir,
            "commit": commit,
        },
    )


@mcp.tool()
async def create_dataset(
    name: str, kind: str, claim: bool = True, shapes: bool = True
) -> dict:
    """Create a new, empty dataset on the backend. There is no drop tool.

    ``kind`` is required and is either ``persistent`` (a TDB2 dataset on disk,
    survives a Fuseki restart) or ``memory`` (in-memory: the contents are lost
    when Fuseki restarts). Ask the person which they want rather than guessing,
    and confirm the name with them too: the bridge cannot remove a dataset once
    it exists, so a mistake is theirs to clean up in Fuseki by hand.

    ``name``: 1-64 characters, lower-case letters, digits, hyphen or underscore,
    starting with a letter.

    Allowed only for a caller whose identity is established AND listed in the
    operator's ``DATASET_CREATORS`` -- anyone else gets a 403, and that is the
    same answer whatever the name. Being admin on some dataset is not enough.

    By default the caller becomes the new dataset's owner (``claim=true``), so
    nobody else can take it in the gap. Pass ``claim=false`` to create it without
    writing anything -- for instance to add it to the bank's ``bankScopedDatasets``
    first, so its graphs use the ``urn:{bank}:{dataset}:{role}`` convention from
    the very first write. The result reports ``bankScoped`` and the graph IRIs
    the dataset will use.

    Fuseki cannot create a dataset with a SHACL endpoint through its API, so
    the bridge adds one to the dataset's configuration file, and it only
    exists after Fuseki is RESTARTED (``restartRequired`` in the result;
    ``shaclEnabled`` is true once it is live). Until then a SHACL-gated bridge
    cannot write to the dataset -- tell the person to restart Fuseki. Unless
    ``shapes=false``, the bundled identity/delegation/fluent shapes are
    seeded into the shapes graph at once. Pass ``shapes=false`` too when you
    want a completely untouched dataset.

    Does not switch you to the new dataset; call ``switch_dataset`` for that.
    """
    return await _call(
        "POST", "/datasets", json_body={"name": name, "kind": kind, "claim": claim, "shapes": shapes}
    )


@mcp.tool()
async def seed_dataset_shapes(claim: bool = False) -> dict:
    """Put the bundled shapes back into the ACTIVE dataset, if its shapes graph is empty.

    For a dataset that came back empty: an in-memory dataset after a Fuseki
    restart keeps its name and shacl endpoint but loses its shapes and its
    owner, and a SHACL-gated bridge then rejects every write to it (400,
    "shapes graph is empty or absent") with no way to push the shapes through
    the gated routes. This restores them.

    ``claim=true`` also makes you the owner if the dataset has none, so one call
    restores both. It never overwrites: a shapes graph with any content is left
    alone (``seeded: false`` and why), and a dataset that already has an owner
    reports ``claimError`` rather than changing hands.

    Same gate as ``create_dataset``: only a caller whose identity is established
    and listed in the operator's ``DATASET_CREATORS``; anyone else gets a 403.
    Acts on the active dataset -- ``switch_dataset`` first.
    """
    return await _call("POST", "/admin/dataset/shapes", json_body={"claim": claim})
