"""
schema.py — Schema validation for adapter outputs.
Each adapter must return exactly these fields with the expected types.
Mismatch raises SchemaError which the loop treats as fatal.
"""


class SchemaError(Exception):
    """Raised when adapter output doesn't match expected schema version or structure.
    This should halt the trading loop — if an adapter can't produce reliable data,
    the system should not trade on unreliable data.
    """

    pass


ADAPTER_SCHEMAS = {
    "price": {
        "required_fields": ["schema_version", "asset", "current_price", "candles"],
        "field_types": {
            "schema_version": str,
            "asset": str,
            "current_price": (int, float),
            "candles": list,
        },
    },
    "macro": {
        "required_fields": ["schema_version", "asset", "available", "indicators"],
        "field_types": {
            "schema_version": str,
            "asset": str,
            "available": bool,
            "indicators": dict,
        },
    },
    "news": {
        "required_fields": ["schema_version", "asset", "available", "articles"],
        "field_types": {
            "schema_version": str,
            "asset": str,
            "available": bool,
            "articles": list,
        },
    },
    "onchain": {
        "required_fields": ["schema_version", "asset", "available", "metrics"],
        "field_types": {
            "schema_version": str,
            "asset": str,
            "available": bool,
            "metrics": dict,
        },
    },
}


def validate_adapter_output(adapter_name: str, output: dict) -> bool:
    """Validate adapter output against its schema.

    Args:
        adapter_name: key in ADAPTER_SCHEMAS ('price', 'macro', etc.)
        output: dict returned by the adapter's fetch()

    Returns:
        True if valid

    Raises:
        SchemaError if any field is missing or has wrong type
    """
    schema = ADAPTER_SCHEMAS.get(adapter_name)
    if not schema:
        raise SchemaError(f"No schema registered for adapter '{adapter_name}'")

    for field in schema["required_fields"]:
        if field not in output:
            raise SchemaError(
                f"[{adapter_name}] Missing required field '{field}' in adapter output. "
                f"Got keys: {list(output.keys())}"
            )
        expected_type = schema["field_types"].get(field)
        if expected_type and not isinstance(output[field], expected_type):
            raise SchemaError(
                f"[{adapter_name}] Field '{field}' has wrong type. "
                f"Expected {expected_type.__name__}, got {type(output[field]).__name__}"
            )

    return True
