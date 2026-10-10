"""Regenerate chaos/contracts/<Name>.schema.json from the pydantic models.

Run from chaos/:  python -m common.schemas
"""
from __future__ import annotations

import json
from pathlib import Path

from .config import CHAOS_ROOT
from .contracts import SCHEMA_MODELS

CONTRACTS_DIR = CHAOS_ROOT / "contracts"


def render_schemas() -> dict[str, str]:
    out = {}
    for name, model in SCHEMA_MODELS.items():
        schema = model.model_json_schema(by_alias=True, mode="validation")
        schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{name}.schema.json", **schema}
        out[f"{name}.schema.json"] = json.dumps(schema, indent=2, ensure_ascii=False) + "\n"
    return out


def main() -> None:
    CONTRACTS_DIR.mkdir(exist_ok=True)
    for filename, text in render_schemas().items():
        Path(CONTRACTS_DIR / filename).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote contracts/{filename}")


if __name__ == "__main__":
    main()
