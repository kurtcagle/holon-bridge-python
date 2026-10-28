"""Adding a ``/shacl`` endpoint to a dataset's Fuseki service configuration.

Fuseki's admin API can create a dataset only from a fixed template
(``dbName``/``dbType``); it no longer accepts an uploaded service configuration,
because such a file can read any file on the server. The template has no
``fuseki:shacl`` endpoint, so a dataset created through the API cannot be
validated against.

The only way left to give it one is to edit the configuration file Fuseki wrote
for it (``{FUSEKI_CONFIG_DIR}/{name}.ttl``), and Fuseki reads that file at
startup, so **the endpoint appears after the next restart**. The same
mechanism, and the same ``FUSEKI_CONFIG_DIR`` setting, as
:mod:`holonbridge.provisioning`.

The edit is deliberately small and safe:

* **Append, never rewrite.** Fuseki's own file is read, one statement is added to
  the end, and everything Fuseki generated (database location, dataset type,
  existing endpoints) is left byte-for-byte as it was. The bridge never decides
  where a database lives.
* **Nothing a caller supplies goes into it** except the dataset name, which has
  already passed the strict name check and is only used to find the file.
* **Checked before and after.** The file must contain exactly one
  ``fuseki:Service`` whose ``fuseki:name`` is the dataset; the result must parse;
  it is written to a temporary file and moved into place, so Fuseki never sees
  a half-written one.
* **Idempotent.** A service that already has a shacl endpoint is left alone.

The checks matter because Fuseki aborts startup on a single bad configuration
file rather than skipping it (this took every dataset down once, see
:mod:`holonbridge.provisioning`). The endpoint is written in the same form the
existing datasets use, ``fuseki:endpoint [ fuseki:operation fuseki:shacl ;
fuseki:name "shacl" ]``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from rdflib import RDF, Literal, URIRef
from rdflib.namespace import Namespace

from .turtle import TurtleSyntaxError
from .turtle import parse as parse_turtle

FUSEKI = Namespace("http://jena.apache.org/fuseki#")

#: Base the file is parsed against, so a relative IRI such as ``<#service>`` can
#: be written back out in the same relative form.
_BASE = "http://holonbridge.invalid/service-config"


class ServiceConfigError(Exception):
    """The configuration could not be read or safely changed."""


@dataclass(frozen=True)
class ShaclConfigResult:
    path: Path
    changed: bool  # False: the service already had a shacl endpoint
    restart_required: bool


def config_path(config_dir: Path, name: str) -> Path:
    """``{config_dir}/{name}.ttl``, refusing anything that would leave the directory."""
    path = config_dir / f"{name}.ttl"
    if path.resolve().parent != config_dir.resolve():
        raise ServiceConfigError(f"{name!r} does not name a file inside the configuration directory")
    return path


def _service(text: str, name: str):
    try:
        graph = parse_turtle(text, base=_BASE)
    except TurtleSyntaxError as exc:
        raise ServiceConfigError(f"the configuration does not parse: {exc}") from exc
    services = list(graph.subjects(RDF.type, FUSEKI.Service))
    if len(services) != 1 or not isinstance(services[0], URIRef):
        raise ServiceConfigError(
            f"expected exactly one named fuseki:Service in the configuration, found {len(services)}"
        )
    service = services[0]
    if Literal(name) not in set(graph.objects(service, FUSEKI.name)):
        raise ServiceConfigError(f"the configuration's service is not named {name!r}")
    return graph, service


def _ref(service: URIRef) -> str:
    text = str(service)
    return f"<{text[len(_BASE):]}>" if text.startswith(_BASE) else f"<{text}>"


def has_shacl(text: str, name: str) -> bool:
    graph, service = _service(text, name)
    return any(
        (endpoint, FUSEKI.operation, FUSEKI.shacl) in graph
        for endpoint in graph.objects(service, FUSEKI.endpoint)
    )


def with_shacl(text: str, name: str) -> str:
    """``text`` with a ``shacl`` endpoint appended to the service (unchanged if it has one)."""
    if has_shacl(text, name):
        return text
    _, service = _service(text, name)
    addition = (
        "\n# added by holon-bridge: SHACL endpoint (takes effect when Fuseki restarts)\n"
        f"{_ref(service)} <{FUSEKI.endpoint}> "
        f'[ <{FUSEKI.operation}> <{FUSEKI.shacl}> ; <{FUSEKI.name}> "shacl" ] .\n'
    )
    result = text.rstrip("\n") + "\n" + addition
    if not has_shacl(result, name):  # never write a file we could not read back
        raise ServiceConfigError("the edited configuration did not contain the shacl endpoint")
    return result


def enable_shacl(config_dir: Path, name: str) -> ShaclConfigResult:
    """Add a shacl endpoint to ``{config_dir}/{name}.ttl``. Raises :class:`ServiceConfigError`."""
    path = config_path(config_dir, name)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ServiceConfigError(f"no configuration file at {path}") from exc
    except OSError as exc:
        raise ServiceConfigError(f"cannot read {path}: {exc}") from exc

    new_text = with_shacl(text, name)
    if new_text == text:
        return ShaclConfigResult(path, changed=False, restart_required=False)

    tmp = path.with_name(path.name + ".hb-tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ServiceConfigError(f"cannot write {path}: {exc}") from exc
    return ShaclConfigResult(path, changed=True, restart_required=True)
