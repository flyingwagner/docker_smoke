"""Load optional Codex model metadata without using it as an allowlist."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_model_catalog(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, (str, Path)):
        try:
            value = json.loads(Path(value).expanduser().read_text())
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError("model_catalog must reference a readable JSON file") from error
    if not isinstance(value, dict):
        raise ValueError("model_catalog must be a JSON object or JSON file path")
    try:
        copied = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise ValueError("model_catalog must contain valid JSON values") from error
    if copied != value:
        raise ValueError("model_catalog must be representable as JSON without changes")
    models = copied.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("model_catalog.models must be a nonempty list")
    slugs = []
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("model_catalog.models entries must be JSON objects")
        slug = model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("model_catalog.models entries require a nonempty slug")
        slugs.append(slug)
    if len(slugs) != len(set(slugs)):
        raise ValueError("model_catalog.models must not contain duplicate slugs")
    return copied


__all__ = ["load_model_catalog"]
