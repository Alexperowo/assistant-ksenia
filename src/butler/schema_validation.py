from __future__ import annotations

from typing import Any


class SchemaValidationError(ValueError):
    """Raised when untrusted JSON does not match a supported tool schema."""


def _location(path: tuple[str, ...]) -> str:
    if not path:
        return "value"
    return "property " + ".".join(repr(part) for part in path)


def _type_name(expected: str) -> str:
    return {
        "array": "array",
        "boolean": "boolean",
        "integer": "integer",
        "number": "number",
        "object": "object",
        "string": "string",
    }.get(expected, expected)


def validate_json_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> None:
    """Validate the deliberately small JSON-Schema subset used by tool calls.

    This is an execution-boundary validator, not a general JSON-Schema engine.
    Unsupported or malformed schema constructs fail closed so a future schema
    cannot silently become less strict.
    """

    if not isinstance(schema, dict):
        raise SchemaValidationError("tool schema must be an object")

    alternatives = schema.get("anyOf")
    if alternatives is not None:
        if not isinstance(alternatives, list) or not alternatives:
            raise SchemaValidationError("tool schema anyOf must be a non-empty array")
        for alternative in alternatives:
            try:
                validate_json_schema(value, alternative, path=path)
            except SchemaValidationError:
                continue
            return
        raise SchemaValidationError(
            f"{_location(path)} must match at least one allowed shape"
        )

    alternatives = schema.get("oneOf")
    if alternatives is not None:
        if not isinstance(alternatives, list) or not alternatives:
            raise SchemaValidationError("tool schema oneOf must be a non-empty array")
        matches = 0
        for alternative in alternatives:
            try:
                validate_json_schema(value, alternative, path=path)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(
                f"{_location(path)} must match exactly one allowed shape"
            )
        return

    expected = schema.get("type")
    if not isinstance(expected, str):
        raise SchemaValidationError("tool schema is missing a supported type")

    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": type(value) is bool,
        "integer": type(value) is int,
        "number": type(value) in {int, float},
    }.get(expected)
    if valid_type is None:
        raise SchemaValidationError(f"unsupported tool schema type: {expected}")
    if not valid_type:
        raise SchemaValidationError(
            f"{_location(path)} must be {_type_name(expected)}"
        )

    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list):
            raise SchemaValidationError("tool schema enum must be an array")
        if value not in enum:
            raise SchemaValidationError(f"{_location(path)} has an unsupported value")

    if expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise SchemaValidationError("tool object schema is malformed")
        for name in required:
            if not isinstance(name, str):
                raise SchemaValidationError("tool schema required names must be strings")
            if name not in value:
                raise SchemaValidationError(f"missing required property {name!r}")
        if schema.get("additionalProperties") is False:
            unexpected = sorted(str(name) for name in value if name not in properties)
            if unexpected:
                raise SchemaValidationError(f"unexpected property {unexpected[0]!r}")
        for name, item in value.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                validate_json_schema(item, child_schema, path=path + (str(name),))
        return

    if expected == "array":
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None and len(value) < int(minimum):
            raise SchemaValidationError(
                f"{_location(path)} must contain at least {int(minimum)} items"
            )
        if maximum is not None and len(value) > int(maximum):
            raise SchemaValidationError(
                f"{_location(path)} must contain at most {int(maximum)} items"
            )
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                validate_json_schema(item, item_schema, path=path + (str(index),))
        return

    if expected == "string":
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < int(minimum):
            raise SchemaValidationError(
                f"{_location(path)} must contain at least {int(minimum)} characters"
            )
        if maximum is not None and len(value) > int(maximum):
            raise SchemaValidationError(
                f"{_location(path)} must contain at most {int(maximum)} characters"
            )
        return

    if expected in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < minimum:
            raise SchemaValidationError(
                f"{_location(path)} must be at least {minimum}"
            )
        if maximum is not None and value > maximum:
            raise SchemaValidationError(
                f"{_location(path)} must be at most {maximum}"
            )
