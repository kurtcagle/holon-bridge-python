"""Identity tools: whoami, persona switching, persona listing."""

from __future__ import annotations

from ..session import mcp, _call


@mcp.tool()
async def whoami() -> dict:
    """Which Person the bridge resolved this session's credential as.

    Requires nothing beyond a resolved identity -- no grant on anything.
    Exists because every founder currently holds identical grants, so no
    permission-based test can tell them apart; this answers "who am I"
    directly instead of by inference from what does or doesn't succeed.

    Also reports ``persona``/``personaSource`` -- your active persona
    override for the current dataset, and where it came from
    (``explicit``/``persisted``/``env``/``none``). See ``switch_persona``.
    """
    return await _call("GET", "/whoami")


@mcp.tool()
async def switch_persona(name: str = "") -> dict:
    """Switch your active persona for the current dataset.

    Per-person, not per-process -- unlike ``switch_dataset``/``switch_bank``,
    this holds no state in this MCP process. "Which persona" is a
    question about *who's asking*, so the bridge keys it by the Person your
    credential resolves to (see ``whoami``); switching here can never move
    another caller's persona, and can never be pointed at one.

    Gated on holding a ``holon:Home`` under the named persona -- existence
    of the persona alone is not enough. The response distinguishes a
    misspelled name (``not_found``) from a real persona you're not a
    member of (``refused``); both come back as ``ok: false`` with a
    ``note`` saying which. Call ``list_personas`` first if you're not sure
    which names would actually succeed for you.

    Pass an empty string (or omit ``name``) to clear your override for
    this dataset and fall back to ground truth only.
    """
    return await _call("POST", "/persona/switch", json_body={"name": name})


@mcp.tool()
async def list_personas() -> dict:
    """Every holon:Persona in the current dataset, and whether you hold a
    Home under it -- i.e. which names ``switch_persona`` would actually
    accept from you right now."""
    return await _call("GET", "/persona/list")
