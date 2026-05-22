"""Generate combined JSON Schema from Pydantic models.

Usage: python schema_gen.py <module>

Reads the SCHEMA_MODELS dict below and writes a combined JSON Schema
to stdout.  The .pth file in the venv ensures modules like
relay_core.relay_models are importable.

Each entry in SCHEMA_MODELS may resolve to either a Pydantic
``BaseModel`` subclass *or* a discriminated-union ``TypeAlias`` such as
``Annotated[A | B, Field(discriminator="type")]`` — both are converted
to JSON Schema via Pydantic's ``TypeAdapter``.
"""

import importlib
import inspect
import json
import sys
import types
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, TypeAdapter


def generate_schema(module: types.ModuleType, names: list[str]) -> None:
    """Build a combined JSON Schema for *names* defined in *module*.

    For each name we resolve it on the module then ask Pydantic for the
    JSON Schema. Pydantic handles both BaseModel subclasses and
    Annotated-union TypeAliases via ``TypeAdapter``.
    """
    defs: dict[str, object] = {}
    refs: list[dict[str, str]] = []

    for name in names:
        value = getattr(module, name)
        adapter = TypeAdapter(value)
        s = adapter.json_schema(ref_template="#/$defs/{model}")
        defs.update(s.get("$defs", {}))
        if (
            inspect.isclass(value)
            and issubclass(value, BaseModel)
            and value.__name__ != name
        ):
            # SCHEMA_MODELS entry is a Python alias to a class with a
            # different __name__ (e.g. ``WebhookPayload = WebhookPayloadTrades``).
            # Emit our entry as a $ref so json-schema-to-typescript produces
            # ``export type Alias = Canonical`` instead of a duplicate interface.
            canonical = value.__name__
            if canonical not in defs:
                defs[canonical] = {k: v for k, v in s.items() if k != "$defs"}
            # ``allOf: [$ref]`` (rather than a bare $ref) forces
            # json-schema-to-typescript to emit a named alias declaration
            # ``export type Alias = Canonical`` instead of silently
            # collapsing the entry into its target.
            defs[name] = {"allOf": [{"$ref": f"#/$defs/{canonical}"}]}
        else:
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
    """Hoist named Literal aliases to top-level $defs and references.

    Two effects:
    1. Inline enum arrays inside model defs are replaced with ``$ref``
       to the matching alias — so consumers see ``BuySell`` rather than
       a duplicated ``"buy" | "sell"`` everywhere.
    2. Every named alias is appended to the umbrella ``anyOf`` ref list
       so ``json-schema-to-typescript`` always emits an
       ``export type Alias = ...`` declaration, even for aliases no
       model references inline.
    """
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

    # Force every named alias to appear in the TS output by adding it
    # to the umbrella anyOf — json-schema-to-typescript only emits
    # types referenced from a reachable schema entry.
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        existing_refs = {r.get("$ref") for r in any_of if isinstance(r, dict)}
        for name in sorted(alias_names):
            ref = f"#/$defs/{name}"
            if ref not in existing_refs:
                any_of.append({"$ref": ref})


def _replace_inline_enums(obj: object, aliases: dict[frozenset[str], str]) -> None:
    """Recursively replace any matching inline string enum/const schema with $ref.

    Handles both multi-value (``{"enum": [...], "type": "string"}``) and
    single-value (``{"const": "val", "type": "string"}``) Literal schemas.
    Pydantic v2 emits ``const`` for single-value Literals, so both forms
    must be matched to produce a ``$ref`` to the named alias.
    """
    if isinstance(obj, dict):
        matched: frozenset[str] | None = None
        strip_keys: tuple[str, ...] = ()

        enum = obj.get("enum")
        if enum and obj.get("type") == "string":
            matched = frozenset(enum)
            strip_keys = ("enum", "type")
        else:
            const = obj.get("const")
            if isinstance(const, str) and obj.get("type") == "string":
                matched = frozenset({const})
                strip_keys = ("const", "type")

        if matched is not None:
            alias_name = aliases.get(matched)
            if alias_name is not None:
                ref = {"$ref": f"#/$defs/{alias_name}"}
                extra = {k: v for k, v in obj.items() if k not in strip_keys}
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
# nested models are pulled in automatically via $ref. Entries may be
# either Pydantic BaseModel subclasses or discriminated-union
# TypeAliases — Pydantic's TypeAdapter handles both.
SCHEMA_MODELS: dict[str, list[str]] = {
    "shared": [
        "Trade",
        "Fill",
    ],
    "relay_core.relay_models": [
        "WebhookPayload",
        "WebhookPayloadTrades",
        "RunPollResponse",
        "HealthResponse",
    ],
    "market_data.models.dividends": [
        "DividendsUpcomingQuery",
        "DividendsUpcomingItem",
        "DividendsUpcomingResponse",
    ],
}


def _resolve_or_die(mod: types.ModuleType, name: str) -> Any:
    try:
        return getattr(mod, name)
    except AttributeError as exc:
        raise SystemExit(
            f"ERROR: model {name!r} not found in module {mod.__name__!r}. "
            "Update SCHEMA_MODELS in schema_gen.py or restore the renamed export."
        ) from exc


def _validate_schema_compatible(name: str, value: Any, mod_name: str) -> None:
    """Ensure *value* is something ``generate_schema`` can actually use.

    Accepts:
    - Pydantic ``BaseModel`` subclasses (fast-path).
    - Typing constructs that represent a real type: ``Annotated[...]``,
      ``Union[...]``, ``Literal[...]``, generic aliases, etc. These all
      return non-``None`` from :func:`typing.get_origin`.

    Rejects everything else (functions, lambdas, plain strings, ints,
    module globals, non-Pydantic classes) with a targeted ``SystemExit``.
    ``TypeAdapter`` alone is too permissive (it silently accepts a bare
    lambda or a string as a forward-ref) so we gate on ``get_origin``
    first and only then probe Pydantic for a final sanity check.
    """
    if inspect.isclass(value) and issubclass(value, BaseModel):
        return
    if get_origin(value) is None:
        raise SystemExit(
            f"ERROR: {name!r} in {mod_name!r} is not schema-compatible "
            f"(must be a Pydantic BaseModel subclass or a typing construct "
            f"such as Annotated, Union, or Literal — got "
            f"{type(value).__name__} {value!r})."
        )
    try:
        TypeAdapter(value)
    except Exception as exc:
        raise SystemExit(
            f"ERROR: {name!r} in {mod_name!r} is a typing construct but "
            f"Pydantic's TypeAdapter cannot build a schema for it: {exc}"
        ) from exc


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

    for n in model_names:
        value = _resolve_or_die(mod, n)
        _validate_schema_compatible(n, value, mod_name)

    generate_schema(mod, model_names)
