"""Stable workbook selector encoding shared by build, inspect, and validation."""

from __future__ import annotations


def defined_name_component(value: str) -> str:
    """Encode a selector component for an Excel defined name.

    The manifest keeps the exact user-facing ID. Excel defined names cannot contain
    characters such as spaces or dots, so those characters are represented as
    underscores in the package. Builders reject collisions after this encoding.
    """

    return "".join(character if character.isalnum() else "_" for character in value)


def stable_sheet_name(sheet_id: str) -> str:
    return f"aer_sheet_{defined_name_component(sheet_id)}"


def stable_cell_name(sheet_id: str, cell_id: str) -> str:
    return f"aer_{defined_name_component(sheet_id)}_{defined_name_component(cell_id)}"


def normalize_stable_selector(selector: str) -> str:
    """Normalize manifest and package-side selectors for exact comparison."""

    prefix = "sheet:id="
    if not selector.startswith(prefix):
        return selector
    value = selector.removeprefix(prefix)
    marker = "/cell:id="
    if marker not in value:
        return f"{prefix}{defined_name_component(value)}"
    sheet_id, cell_id = value.split(marker, 1)
    return f"{prefix}{defined_name_component(sheet_id)}{marker}{defined_name_component(cell_id)}"
