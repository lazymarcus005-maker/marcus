import hashlib
import os
import tempfile
from pathlib import Path


def content_hash(content: str | bytes) -> str:
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def atomic_write_text(path: Path, content: str) -> dict[str, str | int | None]:
    """Write in the destination directory, fsync, then atomically replace."""
    previous = path.read_bytes() if path.is_file() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "bytes_written": len(content.encode()),
        "pre_image_hash": content_hash(previous) if previous is not None else None,
        "post_image_hash": content_hash(content),
    }
