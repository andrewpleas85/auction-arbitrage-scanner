"""File-integrity guard: catch an emptied or broken critical source file before it does damage.

  * verify_critical_files(): used at scanner startup (and by `python integrity.py`). Reports files
    that are missing, empty, or not valid Python.
  * safe_write(): atomic write for edit scripts. Refuses to replace an existing file with empty
    (or near-empty) content, and never leaves a half-written file behind.
"""
import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CRITICAL_FILES = (
    "main.py", "report.py", "webpage.py", "config.py", "db.py", "models.py", "integrity.py",
    "engine/valuation.py", "engine/title_cleaner.py", "engine/details.py", "engine/knowledge.py", "engine/liquidity.py", "catalog.py", "engine/watchlist.py", "engine/matching.py",
    "clients/ebay.py", "clients/gsa.py", "clients/propertyroom.py",
    "alerting/discord.py",
)
MIN_BYTES = 40  # any real module is bigger than this


class IntegrityError(RuntimeError):
    pass


def verify_critical_files(root: Path | None = None, files: tuple[str, ...] = CRITICAL_FILES) -> list[str]:
    """Problems found (empty list = healthy)."""
    base = Path(root) if root else ROOT
    problems = []
    for rel in files:
        path = base / rel
        if not path.exists():
            problems.append(f"{rel}: missing")
            continue
        text = path.read_text(errors="replace")
        if len(text.strip()) < MIN_BYTES:
            problems.append(f"{rel}: empty or near-empty ({path.stat().st_size} bytes)")
            continue
        try:
            ast.parse(text, filename=rel)
        except SyntaxError as e:
            problems.append(f"{rel}: syntax error at line {e.lineno}: {e.msg}")
    return problems


def safe_write(path: str | Path, content: str, *, min_bytes: int = MIN_BYTES) -> None:
    """Atomically write `content` to `path`; refuse to replace a file with (near-)empty content."""
    path = Path(path)
    if len(content.strip()) < min_bytes and (path.exists() and path.stat().st_size >= min_bytes
                                             or path.name in {Path(c).name for c in CRITICAL_FILES}):
        raise IntegrityError(f"refusing to write {len(content)} bytes over {path}")
    if path.suffix == ".py":
        try:
            ast.parse(content, filename=str(path))
        except SyntaxError as e:
            raise IntegrityError(f"refusing to write invalid Python to {path}: line {e.lineno}: {e.msg}") from e
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def require_healthy() -> None:
    problems = verify_critical_files()
    if problems:
        raise SystemExit("integrity check failed, refusing to start:\n  " + "\n  ".join(problems))


if __name__ == "__main__":
    found = verify_critical_files()
    print("\n".join(found) if found else f"ok: {len(CRITICAL_FILES)} critical files healthy")
    sys.exit(1 if found else 0)
