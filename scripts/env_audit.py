#!/usr/bin/env python3
"""List the environment variables a .env file defines, what each is for, and
which ones nothing in this codebase reads any more -- without ever reading,
printing or deriving anything from a value.

    python scripts/env_audit.py --env /path/to/.env
    python scripts/env_audit.py --env .env --format json --out report.json
    python scripts/env_audit.py --env .env.example        # audit the documentation itself

Run it on the machine that holds the real ``.env``; the report it writes has
names, descriptions and file locations only, so it is safe to paste or upload.

What "value-free" means here, concretely: the ``.env`` parser keeps the text
before the ``=`` on each line. After the ``=`` it looks at one thing, whether a
quote opens that does not close on the same line, so a multi-line value is not
mistaken for several definitions; nothing derived from a value is stored or
reported (not whether it is empty, how long it is, or whether it is quoted).
There is a test that
runs the whole tool over a file full of sentinel secrets and fails if any
fragment of one reaches any output format.

How the verdicts are reached (standard library only; no import of holonbridge,
so it runs from a bare checkout and has no side effects):

* ``read``       a Python file under holonbridge/, holonbridge_mcp/ or scripts/
                 reads the name through ``os.getenv`` / ``os.environ`` or
                 through a helper that does (``_flag("NAME", ...)``), found with
                 the AST rather than a text search.
* ``mentioned``  the name appears as a string literal in that code but not as a
                 direct read (for example a tuple of names looped over with
                 ``os.getenv``). Counted as current: such a literal is almost
                 always a read, and calling it an orphan would be the worse error.
* ``tooling``    the name appears in a shell/PowerShell/YAML/TOML/Docker file.
* ``docs only``  the name appears in ``.env.example`` and nowhere else.

A name in your ``.env`` that is ``docs only`` or not found anywhere is an
orphan *candidate*; one found only in scripts is reported separately. The tool can only see this repository: a
variable used by Fuseki, ngrok, Claude Desktop or your shell would also look
orphaned here, and credentials a registered agent endpoint points at with
``env:NAME`` live in the graph, not in the code (the ``HB_AGENT_`` family below
is the one the code knows about). Treat the list as a prompt to check, not a
list to delete.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

CODE_DIRS = ("holonbridge", "holonbridge_mcp", "scripts")
TOOLING_SUFFIXES = {".sh", ".ps1", ".bat", ".cmd", ".yml", ".yaml", ".toml", ".cfg", ".json"}
TOOLING_NAMES = {"dockerfile", "makefile", "compose.yml", "docker-compose.yml"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "tests", "docs", "dist", "build"}
MAX_FILE_BYTES = 1_000_000

#: Name prefixes the code resolves at run time rather than by literal, so a
#: name here is "current" without a literal read. Add to this when a new
#: dynamic family appears.
DYNAMIC_FAMILIES = {
    "HB_AGENT_": "agent endpoint credentials, referenced as env:NAME from a registered "
    "endpoint (AGENT_CREDENTIAL_PREFIX)",
}

NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_ASSIGN = re.compile(rf"^\s*(?:export\s+)?({NAME})\s*=")
_COMMENTED_ASSIGN = re.compile(rf"^#\s*({NAME})\s*=")
_CONTINUATION = re.compile(r"^#\s{6,}(\S.*)$")
_UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")


# --- .env parsing: names only ---------------------------------------------------


def env_names(path: Path) -> dict[str, int]:
    """``{name: times defined}`` from a .env file. Values are never kept.

    The only state carried across lines is whether a quoted value is still open,
    so that a line inside a multi-line value is not mistaken for a definition.
    """
    counts: dict[str, int] = {}
    open_quote: str | None = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if open_quote:
                if _closes(line, open_quote):
                    open_quote = None
                continue
            match = _ASSIGN.match(line)
            if not match or line.lstrip().startswith("#"):
                continue
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
            rest = line[match.end():].lstrip()
            if rest[:1] in ("'", '"') and not _closes(rest[1:], rest[0]):
                open_quote = rest[0]
    return counts


def _closes(text: str, quote: str) -> bool:
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
        elif ch == "\\" and quote == '"':
            escaped = True
        elif ch == quote:
            return True
    return False


# --- descriptions from .env.example --------------------------------------------


_BANNER = re.compile(r"^#\s*-{2,}.*$|^#\s*={3,}.*$")
_HANGING_ANY = re.compile(rf"^#\s{{2,}}({NAME})\s+(\S.*)$")


def example_descriptions(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Names documented in ``.env.example``, each with the comment text that explains it.

    A hanging entry (``#   NAME   explanation`` plus indented continuation lines)
    is that name's own description. Otherwise a ``NAME=`` or ``# NAME=`` line takes
    the plain comment block directly above it, if no other name has already used
    that block. Section banners (``# --- auth ---``) never become descriptions.

    Returns ``(own, from_block)``: the second kind is a guess at which comment
    belongs to the name, so the caller ranks it below a comment in the code.
    """
    if not path.is_file():
        return {}, {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    candidates = {m.group(1) for ln in lines
                  if (m := _ASSIGN.match(ln) or _COMMENTED_ASSIGN.match(ln)) and _UPPER.match(m.group(1))}

    documented: dict[str, str] = {}
    hanging: dict[str, str] = {}
    block: list[str] = []
    current: str | None = None
    for raw in lines:
        line = raw.rstrip()
        if _BANNER.match(line):
            block, current = [], None
            continue
        cont = _CONTINUATION.match(line)
        if cont and current:  # checked first: a wrapped line may itself begin with a name
            hanging[current] += " " + cont.group(1).strip()
            continue
        hang = _HANGING_ANY.match(line)
        if hang and hang.group(1) in candidates:
            current = hang.group(1)
            hanging[current] = hang.group(2).strip()
            continue
        current = None
        assign = _ASSIGN.match(line) or _COMMENTED_ASSIGN.match(line)
        if assign and _UPPER.match(assign.group(1)):
            documented.setdefault(assign.group(1), _clean(block) if block else "")
            block = []
        elif line.startswith("#"):
            block.append(line)
        else:
            block = []
    return {n: _shorten(t) for n, t in hanging.items()}, documented


def _clean(block: list[str]) -> str:
    lines = [re.sub(r"^#+\s?", "", line).strip() for line in block]
    return _shorten(" ".join(ln for ln in lines if ln and not set(ln) <= set("-=# ")))


def _shorten(text: str, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: stop + 1] if stop > limit // 2 else cut.rsplit(" ", 1)[0] + " ...").strip()


# --- what the code reads ---------------------------------------------------------


@dataclass
class CodeUse:
    read: dict[str, list[str]] = field(default_factory=dict)  # name -> ["file:line", ...]
    mentioned: dict[str, list[str]] = field(default_factory=dict)
    dynamic: list[str] = field(default_factory=list)  # reads whose name is not a literal
    notes: dict[str, str] = field(default_factory=dict)  # name -> comment written next to the read


_ENV_CALLS = {"os.getenv", "getenv", "os.environ.get", "environ.get",
              "os.environ.setdefault", "os.environ.pop", "environ.setdefault", "environ.pop"}


def _is_env_read(node: ast.Call) -> bool:
    try:
        return ast.unparse(node.func) in _ENV_CALLS
    except (ValueError, TypeError, RecursionError):  # pragma: no cover - not a call we know
        return False


def _literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def scan_code(repo: Path) -> CodeUse:
    use = CodeUse()
    files: list[tuple[Path, ast.AST]] = []
    for name in CODE_DIRS:
        base = repo / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_DIRS & set(path.relative_to(repo).parts[:-1]):
                continue
            try:
                files.append((path, ast.parse(path.read_text(encoding="utf-8", errors="replace"))))
            except SyntaxError:  # a file that does not parse cannot be read for names
                continue

    helpers = _find_helpers(tree for _, tree in files)
    sources = {path: path.read_text(encoding="utf-8", errors="replace").splitlines()
               for path, _ in files}

    def add(bucket: dict[str, list[str]], key: str, where: str) -> None:
        bucket.setdefault(key, []).append(where)

    for path, tree in files:
        rel = path.relative_to(repo).as_posix()
        helper_bodies = {id(n) for n in ast.walk(tree)
                         if isinstance(n, ast.FunctionDef) and n.name in helpers}
        inside_helper = {id(c) for n in ast.walk(tree) if id(n) in helper_bodies
                         for c in ast.walk(n)}
        for node in ast.walk(tree):
            where = f"{rel}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Call):
                if _is_env_read(node) and node.args:
                    literal = _literal(node.args[0])
                    if literal is not None:
                        add(use.read, literal, where)
                    elif id(node) not in inside_helper:
                        use.dynamic.append(where)
                else:
                    callee = _callee_name(node)
                    if callee in helpers and node.args:
                        literal = _literal(node.args[0])
                        if literal is not None:
                            add(use.read, literal, where)
            elif isinstance(node, ast.Subscript):
                target = ast.unparse(node.value)
                if target in ("os.environ", "environ"):
                    literal = _literal(node.slice)
                    if literal is not None:
                        add(use.read, literal, where)
            elif isinstance(node, ast.Compare):
                if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                    literal = _literal(node.left)
                    comparator = node.comparators[0]
                    if literal is not None and ast.unparse(comparator) in ("os.environ", "environ"):
                        add(use.read, literal, where)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and re.fullmatch(NAME, node.value)):
                add(use.mentioned, node.value, where)
    for path, tree in files:
        _collect_notes(use, tree, sources[path], helpers)
    return use


def _comment_above(source: list[str], lineno: int) -> str:
    """The contiguous ``#`` comment directly above 1-based ``lineno``."""
    found: list[str] = []
    index = lineno - 2
    while index >= 0 and source[index].lstrip().startswith("#"):
        found.append(re.sub(r"^\s*#+\s?", "", source[index]).strip())
        index -= 1
    found = [ln for ln in reversed(found) if ln and not set(ln) <= set("-=# ") and not _BANNER.match("# " + ln)]
    return _shorten(" ".join(found))


def _read_literals(node: ast.AST, helpers: set[str]) -> list[str]:
    names: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and sub.args and (
            _is_env_read(sub) or _callee_name(sub) in helpers
        ):
            literal = _literal(sub.args[0])
            if literal is not None:
                names.append(literal)
    return names


def _collect_notes(use: CodeUse, tree: ast.AST, source: list[str], helpers: set[str]) -> None:
    """Attach the comment a developer left for each variable: the comment above the
    Settings field it feeds, else the comment above the short statement that reads it."""
    field_notes: dict[str, str] = {}
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for item in cls.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    text = _comment_above(source, item.lineno)
                    if text:
                        field_notes[item.target.id] = text
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in field_notes:
                    for name in _read_literals(kw.value, helpers):
                        use.notes.setdefault(name, field_notes[kw.arg])
    for node in ast.walk(tree):
        short = (isinstance(node, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return))
                 and (node.end_lineno or node.lineno) - node.lineno <= 8)
        if short:
            text = _comment_above(source, node.lineno)
            if text:
                for name in _read_literals(node, helpers):
                    use.notes.setdefault(name, text)


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _find_helpers(trees) -> set[str]:
    """Functions that read the environment under a name passed in as a parameter,
    e.g. ``def _flag(key, default): os.getenv(key)`` -- calls to these with a
    literal first argument are reads of that literal."""
    helpers: set[str] = set()
    for tree in trees:
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or not func.args.args:
                continue
            params = {a.arg for a in func.args.args}
            for node in ast.walk(func):
                if isinstance(node, ast.Call) and _is_env_read(node) and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Name) and arg.id in params:
                        helpers.add(func.name)
    return helpers


def scan_tooling(repo: Path, names: set[str]) -> dict[str, list[str]]:
    """Whole-word mentions of ``names`` in non-Python tooling files."""
    found: dict[str, list[str]] = {}
    if not names:
        return found
    pattern = re.compile(r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, sorted(names))) + r")(?![A-Za-z0-9_])")
    for path in sorted(repo.rglob("*")):
        rel = path.relative_to(repo)
        if SKIP_DIRS & set(rel.parts[:-1]) or not path.is_file():
            continue
        lowered = path.name.lower()
        if not (path.suffix.lower() in TOOLING_SUFFIXES or lowered in TOOLING_NAMES
                or lowered.startswith("dockerfile")):
            continue
        if lowered == ".env.example" or path.stat().st_size > MAX_FILE_BYTES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in pattern.finditer(line):
                found.setdefault(match.group(1), []).append(f"{rel.as_posix()}:{number}")
    return found


# --- the report -------------------------------------------------------------------


def family_of(name: str) -> str | None:
    for prefix, why in DYNAMIC_FAMILIES.items():
        if name.startswith(prefix):
            return why
    return None


def build_report(env_path: Path, repo: Path) -> dict:
    defined = env_names(env_path)
    own, from_block = example_descriptions(repo / ".env.example")
    documented = {**from_block, **own}
    use = scan_code(repo)
    tooling = scan_tooling(repo, set(defined) | set(documented))

    def status(name: str) -> str:
        if name in use.read:
            return "read"
        if family_of(name):
            return "dynamic family"
        if name in use.mentioned:
            return "mentioned"
        if name in tooling:
            return "tooling"
        if name in documented:
            return "docs only"
        return "unknown"

    def entry(name: str) -> dict:
        text, source = "", ""
        for candidate, label in ((own.get(name), ".env.example"), (use.notes.get(name), "code comment"),
                                 (from_block.get(name), "nearby comment"),
                                 (family_of(name), "family")):
            if candidate:
                text, source = candidate, label
                break
        item = {
            "name": name,
            "description": text,
            "description_source": source,
            "status": status(name),
        }
        if name in use.read:
            item["read_at"] = sorted(set(use.read[name]))[:3]
        if name in tooling:
            item["tooling_at"] = sorted(set(tooling[name]))[:3]
        if defined.get(name, 0) > 1:
            item["defined_times"] = defined[name]
        return item

    current = [entry(n) for n in sorted(defined)
               if status(n) in ("read", "mentioned", "dynamic family")]
    tooling_only = [entry(n) for n in sorted(defined) if status(n) == "tooling"]
    orphans = [entry(n) for n in sorted(defined) if status(n) in ("docs only", "unknown")]
    defaults = [entry(n) for n in sorted(use.read) if n not in defined]
    stale_docs = [entry(n) for n in sorted(documented)
                  if status(n) in ("docs only", "unknown", "tooling")]
    return {
        "env_file": env_path.name,
        "counts": {
            "defined": len(defined),
            "current": len(current),
            "scripts_only": len(tooling_only),
            "orphan_candidates": len(orphans),
            "read_by_code_not_defined": len(defaults),
            "documented_but_not_read": len(stale_docs),
        },
        "current": current,
        "scripts_only": tooling_only,
        "orphan_candidates": orphans,
        "read_by_code_not_defined": defaults,
        "documented_but_not_read": stale_docs,
        "dynamic_reads": sorted(set(use.dynamic)),
        "dynamic_families": DYNAMIC_FAMILIES,
    }


def render_markdown(report: dict) -> str:
    c = report["counts"]
    out = [
        f"# Environment variable audit: `{report['env_file']}`",
        "",
        "Names, descriptions and locations only. No values were read or are shown.",
        "",
        f"- Defined in the file: **{c['defined']}**",
        f"- Current (the Python code reads them): **{c['current']}**",
        f"- Used only by scripts, not by the Python code: **{c['scripts_only']}**",
        f"- Orphan candidates (defined; nothing in this repository refers to them): **{c['orphan_candidates']}**",
        f"- Read by the code but not defined (their defaults apply): **{c['read_by_code_not_defined']}**",
        f"- Documented in `.env.example` but not read by any code: **{c['documented_but_not_read']}**",
        "",
        ("Orphan candidates are only candidates: this tool sees one repository, so a name "
         "used by Fuseki, ngrok, a launcher or your shell will look orphaned here."),
        "",
    ]
    out += _section("Current: defined and read", report["current"], show_read=True)
    out += _section("Used only by scripts (shell, PowerShell, YAML, ...)", report["scripts_only"])
    out += _section("Orphan candidates: defined, nothing here refers to them",
                    report["orphan_candidates"], show_status=True)
    out += _section("Read by the code, not defined in this file", report["read_by_code_not_defined"],
                    show_read=True)
    out += _section("Documented in .env.example, but not read by the Python code",
                    report["documented_but_not_read"], show_status=True)
    if report["dynamic_reads"]:
        out += ["## Reads whose name is computed at run time", "",
                ("These cannot be matched to a name by static analysis, so a variable they use "
                 "could be wrongly listed as an orphan:"), ""]
        out += [f"- `{where}`" for where in report["dynamic_reads"]] + [""]
    out += [("† The comment sits above this name in `.env.example` but may be about the group "
             "it belongs to rather than the name alone."), ""]
    out += ["## Families resolved at run time", ""]
    out += [f"- `{prefix}*`: {why}" for prefix, why in report["dynamic_families"].items()] + [""]
    return "\n".join(out)


def _section(title: str, items: list[dict], *, show_read: bool = False,
             show_status: bool = False) -> list[str]:
    lines = [f"## {title} ({len(items)})", ""]
    if not items:
        return lines + ["None.", ""]
    for item in items:
        text = item["description"] or "(no description in .env.example or in a code comment)"
        if item.get("description_source") == "nearby comment":
            text += " †"
        extra = []
        if show_status:
            extra.append(f"status: {item['status']}")
        if show_read and item.get("read_at"):
            extra.append("read at " + ", ".join(f"`{w}`" for w in item["read_at"][:2]))
        if item.get("tooling_at"):
            extra.append("also in " + ", ".join(f"`{w}`" for w in item["tooling_at"][:2]))
        if item.get("defined_times"):
            extra.append(f"defined {item['defined_times']} times")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"- `{item['name']}`: {text}{suffix}")
    return lines + [""]


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", required=True, type=Path, help="the .env file to audit")
    parser.add_argument("--repo", type=Path, default=here, help="holon-bridge-python checkout")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--out", type=Path, help="write the report here instead of stdout")
    args = parser.parse_args(argv)

    if not args.env.is_file():
        print(f"error: {args.env} is not a file", file=sys.stderr)
        return 2
    if not (args.repo / "holonbridge").is_dir():
        print(f"error: {args.repo} does not look like a holon-bridge-python checkout "
              "(no holonbridge/ directory); pass --repo", file=sys.stderr)
        return 2

    report = build_report(args.env, args.repo)
    text = json.dumps(report, indent=2) if args.format == "json" else render_markdown(report)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
