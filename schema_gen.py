"""Generate combined JSON Schema from Pydantic models.

Usage: python schema_gen.py <module>

Reads the SCHEMA_MODELS dict below and writes a combined JSON Schema
to stdout.  The .pth file in the venv ensures modules like
poller_models are importable.
"""

import importlib
import json
import sys
import types
from typing import Literal, get_args, get_origin

from pydantic import BaseModel


def generate_schema(module: types.ModuleType, models: list[type[BaseModel]]) -> None:
    """Merge JSON Schemas for *models* and write to stdout."""
    schemas = [m.model_json_schema() for m in models]

    defs: dict[str, object] = {}
    refs: list[dict[str, str]] = []
    for model, s in zip(models, schemas, strict=True):
        defs.update(s.get("$defs", {}))
        name = model.__name__
        defs[name] = {k: v for k, v in s.items() if k != "$defs"}
        refs.append({"$ref": f"#/$defs/{name}"})

    schema: dict[str, object] = {"$defs": defs, "anyOf": refs}
    _strip_titles(schema)
    _hoist_literal_aliases(schema, module)

    json.dump(schema, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _strip_titles(obj: object) -> None:
    """Remove per-property 'title' keys so json-schema-to-typescript
    inlines primitive types instead of emitting named aliases."""
    if isinstance(obj, dict):
        for key, val in list(obj.items()):
            if key == "properties" and isinstance(val, dict):
                for prop in val.values():
                    if isinstance(prop, dict):
                        prop.pop("title", None)
            _strip_titles(val)
    elif isinstance(obj, list):
        for item in obj:
            _strip_titles(item)


def _collect_literal_aliases(module: types.ModuleType) -> dict[frozenset[str], str]:
    """Find named Literal type aliases (e.g. Action = Literal["BUY","SELL"])."""
    aliases: dict[frozenset[str], str] = {}
    for name, obj in vars(module).items():
        if get_origin(obj) is Literal:
            args = get_args(obj)
            if all(isinstance(a, str) for a in args):
                aliases[frozenset(args)] = name
    return aliases


def _hoist_literal_aliases(schema: dict[str, object], module: types.ModuleType) -> None:
    """Replace inline enum arrays with $ref to shared type aliases."""
    aliases = _collect_literal_aliases(module)
    if not aliases:
        return

    defs = schema.setdefault("$defs", {})
    if not isinstance(defs, dict):
        raise RuntimeError("schema['$defs'] is not a dict")

    # Add each alias as a $defs entry
    alias_names: set[str] = set()
    for values, name in aliases.items():
        alias_names.add(name)
        if name not in defs:
            defs[name] = {"enum": sorted(values), "type": "string"}

    # Replace inline enums only in model definitions (skip alias defs
    # themselves to avoid self-referencing $ref).
    for name, defn in defs.items():
        if name not in alias_names:
            _replace_inline_enums(defn, aliases)


def _replace_inline_enums(obj: object, aliases: dict[frozenset[str], str]) -> None:
    """Recursively replace any matching inline string enum schema with $ref."""
    if isinstance(obj, dict):
        enum = obj.get("enum")
        if enum and obj.get("type") == "string":
            key = frozenset(enum)
            alias_name = aliases.get(key)
            if alias_name is not None:
                ref = {"$ref": f"#/$defs/{alias_name}"}
                extra = {
                    k: v for k, v in obj.items() if k not in ("enum", "type")
                }
                obj.clear()
                if extra:
                    # Wrap in allOf so json-schema-to-typescript
                    # resolves $ref even with sibling schema metadata present.
                    obj["allOf"] = [ref]
                    obj.update(extra)
                else:
                    obj.update(ref)
        for val in obj.values():
            _replace_inline_enums(val, aliases)
    elif isinstance(obj, list):
        for item in obj:
            _replace_inline_enums(item, aliases)


# Models exported to the JSON Schema / TypeScript types.
# Only top-level response/request wrappers need to be listed here;
# nested models are pulled in automatically via $ref.
SCHEMA_MODELS: dict[str, list[str]] = {
    "shared": [
        "WebhookPayloadTrades",
        "Trade",
        "Fill",
    ],
    "relay_models": [
        "RunPollResponse",
        "HealthResponse",
    ],
}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <module>", file=sys.stderr)
        sys.exit(1)

    mod_name = sys.argv[1]
    mod = importlib.import_module(mod_name)
    model_names = SCHEMA_MODELS.get(mod_name)
    if model_names is None:
        print(f"ERROR: no SCHEMA_MODELS entry for {mod_name!r}", file=sys.stderr)
        sys.exit(1)
    models = []
    for name in model_names:
        if not hasattr(mod, name):
            print(
                f"ERROR: model {name!r} not found in module {mod_name!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        models.append(getattr(mod, name))
    generate_schema(mod, models)
