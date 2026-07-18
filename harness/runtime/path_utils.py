"""Shared path utilities used by both server sandboxed tools and CLI tools."""

from pathlib import Path


def resolve_scoped_path(base: Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``base``, refusing any escape."""
    resolved_base = base.resolve()
    candidate = (resolved_base / relative_path).resolve()
    if not candidate.is_relative_to(resolved_base):
        raise ValueError(f"path {relative_path!r} escapes the scoped directory")
    return candidate
