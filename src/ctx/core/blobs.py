"""Content-addressed blob storage for oversized raw content.

Layout: <root>/blobs/<first-2-hex>/<sha256-hex>. Refs are stored relative to the
data dir (e.g. "blobs/ab/<hash>"). `spill()` writes and fsyncs the file (and its
parent directory) before returning, so a blob always exists on disk before the
event row referencing it is inserted (ADR-004). Blobs hold REDACTED content —
callers must redact before spilling (ADR-012).
"""

import hashlib
import os
from pathlib import Path


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def spill(self, content: str) -> str:
        """Write content (if not already present) and return its ref. Fsynced before return."""
        data = content.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        ref = f"blobs/{digest[:2]}/{digest}"
        path = self.root / ref
        if path.exists():
            return ref
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return ref

    def resolve(self, ref: str) -> Path:
        return self.root / ref

    def read(self, ref: str) -> str:
        return self.resolve(ref).read_text(encoding="utf-8")

    def exists(self, ref: str) -> bool:
        return self.resolve(ref).exists()
