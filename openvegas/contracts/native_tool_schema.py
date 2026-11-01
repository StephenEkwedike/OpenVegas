"""Per-operation local-tool wire contract. No execution or provider authority."""
from __future__ import annotations

GENERIC_V1 = "generic-v1"
GOOGLE_FLAT_V1 = "google-flat-v1"
FLAT_V2 = "flat-v2"

_OPERATIONS = (
    ("Read", "Read a local workspace file.", ("path",), ("max_bytes", "result_content_max_chars")),
    ("Search", "Search text in local workspace files.", ("pattern",), ("path", "max_files", "max_matches")),
    ("Write", "Write a local workspace file; approval is required.", ("filepath", "content"), ("write_mode",)),
    ("FindAndReplace", "Replace exact text in a local file; approval is required.",
     ("filepath", "old_string", "new_string"), ("replace_all",)),
    ("InsertAtEnd", "Append text to a local file; approval is required.", ("filepath", "content"), ()),
    ("Bash", "Run a local shell command through the permission system.", ("command",), ()),
    ("List", "List local workspace directory entries.", (), ("path", "recursive", "max_entries")),
)
_NUMERIC = frozenset({"max_bytes", "result_content_max_chars", "max_files", "max_matches", "max_entries"})
_BOOLEAN = frozenset({"replace_all", "recursive"})
_EXACT_TEXT = frozenset({"path", "filepath", "pattern", "command"})
_MUTATIONS = frozenset({"Write", "FindAndReplace", "InsertAtEnd"})


def _field(name: str) -> dict:
    if name in _NUMERIC:
        return {"type": "integer", "minimum": 1}
    if name in _BOOLEAN:
        return {"type": "boolean"}
    if name == "write_mode":
        return {"type": "string", "enum": ["replace", "append"]}
    return {"type": "string", **({"minLength": 1} if name in _EXACT_TEXT or name == "old_string" else {})}


def flat_v2_definitions() -> list[dict]:
    """Return fresh dictionaries; callers cannot mutate a shared schema."""
    definitions = []
    for name, description, required, optional in _OPERATIONS:
        properties = {key: _field(key) for key in (*required, *optional)}
        properties["timeout_sec"] = {"type": "integer", "minimum": 1, "maximum": 300}
        if name == "Bash":
            properties["shell_mode"] = {"type": "string", "enum": ["read_only", "mutating"]}
        definitions.append({"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False},
        }})
    return definitions


def validate_operation(tool: dict, *, legacy_read_alias: bool = False) -> None:
    """Check semantics without rewriting arguments, aliases, text or limits.

    Legacy schemas remain byte-exact, but do not authorize irrelevant fields or
    conflicting aliases. Existing native binding already rejects those shapes.
    """
    operations = {row[0]: row for row in _OPERATIONS}
    name = tool.get("tool_name")
    if type(name) is not str or name not in operations:
        raise ValueError("Unapproved tool")
    _, _, required, optional = operations[name]
    args = tool.get("arguments")
    if type(args) is not dict:
        raise ValueError("Tool request violates the advertised schema")
    allowed = set(required) | set(optional)
    missing = set(required) - args.keys()
    if name == "Read" and legacy_read_alias:
        allowed.add("filepath")
        if "filepath" in args:
            missing.discard("path")
        if "path" in args and "filepath" in args and args["path"] != args["filepath"]:
            raise ValueError("Tool request violates the advertised schema")
    if missing or args.keys() - allowed:
        raise ValueError("Tool request violates the advertised schema")
    for key, value in args.items():
        expected = int if key in _NUMERIC else bool if key in _BOOLEAN else str
        if (type(value) is not expected
                or (key in _NUMERIC and value <= 0)
                or (key in _EXACT_TEXT and (not value.strip() or value != value.strip()))
                or (key == "old_string" and value == "")
                or (key == "write_mode" and value not in {"replace", "append"})):
            raise ValueError("Tool arguments violate the advertised primitive schema")
    mode, timeout = tool.get("shell_mode", "read_only"), tool.get("timeout_sec", 30)
    if (type(mode) is not str or mode not in {"read_only", "mutating"}
            or (name not in _MUTATIONS and name != "Bash" and mode != "read_only")
            or type(timeout) is not int or not 1 <= timeout <= 300):
        raise ValueError("Tool request violates the advertised schema")
