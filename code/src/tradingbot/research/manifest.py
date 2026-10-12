"""Data manifest: exactly which data files a validation run read.

A validation result is only reproducible if the data under it can be named.
`data/` is gitignored, so the repository cannot say which snapshot produced
a number. A manifest does: for every data file the run read, its path, row
count, first and last date, and SHA-256.

Opt-in, so a run without `--manifest` prints exactly what it printed before:

    if __name__ == "__main__":
        sys.exit(run_with_manifest(main, __file__))

and `parser.add_argument("--manifest", action="store_true", help=MANIFEST_HELP)`
in the script's own parser, so `--help` lists it.

What is recorded, without the script having to declare anything:

- every `pandas.read_parquet` call, with rows and first/last index date taken
  from the frame it returned (parquet is read natively, not through Python's
  `open`, so it is wrapped directly);
- every `DataFrame.to_parquet` under `data/`, marked `written`: a script that
  downloads a missing input, caches it and uses the frame in memory never
  reads the file back, but the run depends on it all the same;
- every other file opened for reading under `data/`, through a Python audit
  hook (`provenance.json`, a CSV, ...). Those have no row count or dates.

The SHA-256 is taken when the file is read (or written), so a file rewritten
later in the run is reported as the run saw it. A partitioned parquet
directory is hashed as its sorted (relative path, file hash) list. A reader
that bypasses both `pandas` and Python's `open` (pyarrow directly, say) is not
seen; none exists in this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from tradingbot.config import REPO_ROOT

DATA_DIR = REPO_ROOT / "data"
MANIFEST_DIR = DATA_DIR / "manifests"
MANIFEST_HELP = (
    "record every data file read (path, rows, first/last date, SHA-256), "
    "print it at the end and write it to data/manifests/<script>.json"
)
_WRITE_MODES = set("wax+")


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    rows: int | None
    first: str | None
    last: str | None
    sha256: str
    access: str = "read"


def sha256_of(path: Path) -> str:
    """SHA-256 of a file, or of a directory as its sorted file hashes."""
    global _BUSY
    outer, _BUSY = _BUSY, True  # our own reads are not the script's
    try:
        if path.is_dir():
            digest = hashlib.sha256()
            for child in sorted(p for p in path.rglob("*") if p.is_file()):
                digest.update(child.relative_to(path).as_posix().encode())
                digest.update(sha256_of(child).encode())
            return digest.hexdigest()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        _BUSY = outer


def _display(path: Path) -> str:
    absolute = Path(os.path.abspath(path))
    try:
        return absolute.relative_to(Path(os.path.abspath(REPO_ROOT))).as_posix()
    except ValueError:
        return absolute.as_posix()


def _under_data(path: Path) -> bool:
    absolute = os.path.normcase(os.path.abspath(path))
    return absolute.startswith(os.path.normcase(os.path.abspath(DATA_DIR)) + os.sep)


def _date_range(frame: pd.DataFrame) -> tuple[str | None, str | None]:
    index = frame.index
    if isinstance(index, pd.DatetimeIndex) and len(index):
        return str(index.min()), str(index.max())
    return None, None


class DataManifest:
    def __init__(self) -> None:
        self._entries: dict[str, ManifestEntry] = {}

    # ---------------------------------------------------------------- record

    def _put(self, entry: ManifestEntry) -> None:
        # What the run READ is what it used: a later write or a bare `open`
        # of the same path does not replace a read with a frame behind it.
        existing = self._entries.get(entry.path)
        if existing is not None and existing.access == "read" and existing.rows is not None:
            return
        self._entries[entry.path] = entry

    def record_frame(self, path: Path, frame: pd.DataFrame, access: str = "read") -> None:
        first, last = _date_range(frame)
        key = _display(path)
        self._put(ManifestEntry(key, len(frame), first, last, sha256_of(Path(path)), access))

    def note_opened(self, path: Path) -> None:
        key = _display(path)
        if key not in self._entries and Path(path).is_file():
            self._entries[key] = ManifestEntry(key, None, None, None, sha256_of(Path(path)))

    def entries(self) -> list[ManifestEntry]:
        """Everything read or written, sorted by path."""
        return [self._entries[key] for key in sorted(self._entries)]

    # ----------------------------------------------------------------- track

    @contextmanager
    def tracking(self) -> Iterator[DataManifest]:
        read_original = pd.read_parquet
        write_original = pd.DataFrame.to_parquet

        def read_parquet(path, *args, **kwargs):
            frame = read_original(path, *args, **kwargs)
            if isinstance(path, str | os.PathLike):
                self.record_frame(Path(path), frame)
            return frame

        def to_parquet(frame, path=None, *args, **kwargs):
            result = write_original(frame, path, *args, **kwargs)
            if isinstance(path, str | os.PathLike) and _under_data(Path(path)):
                self.record_frame(Path(path), frame, access="written")
            return result

        _ACTIVE.append(self)
        _install_audit_hook()
        pd.read_parquet = read_parquet
        pd.DataFrame.to_parquet = to_parquet
        try:
            yield self
        finally:
            pd.read_parquet = read_original
            pd.DataFrame.to_parquet = write_original
            _ACTIVE.remove(self)

    # ---------------------------------------------------------------- output

    def format(self) -> str:
        entries = self.entries()
        if not entries:
            return "  no data files were read"
        width = max(len("path"), *(len(e.path) for e in entries))
        lines = [
            f"  {'path':<{width}}  {'access':<7}  {'rows':>8}  {'first':<19}  {'last':<19}  sha256"
        ]
        for e in entries:
            rows = "-" if e.rows is None else str(e.rows)
            lines.append(
                f"  {e.path:<{width}}  {e.access:<7}  {rows:>8}  {e.first or '-':<19}  "
                f"{e.last or '-':<19}  {e.sha256}"
            )
        return "\n".join(lines)

    def to_dict(self, script: str) -> dict:
        return {
            "script": script,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": [asdict(e) for e in self.entries()],
        }

    def write(self, path: Path, script: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(script), indent=2) + "\n", encoding="utf-8")
        return path


# ------------------------------------------------------------- audit hook

_ACTIVE: list[DataManifest] = []
_HOOKED = False
_BUSY = False


def _audit(event: str, args: tuple) -> None:
    # Called for every audited event in the process: return fast.
    if event != "open" or not _ACTIVE or _BUSY:
        return
    path, mode, flags = args
    if not isinstance(path, str | bytes | os.PathLike):
        return  # a file descriptor
    if isinstance(mode, str):
        if _WRITE_MODES & set(mode):
            return
    elif flags is not None and flags & (os.O_WRONLY | os.O_RDWR):
        return
    target = Path(os.path.abspath(os.fsdecode(path)))
    if _under_data(target):
        _ACTIVE[-1].note_opened(target)


def _install_audit_hook() -> None:
    # Audit hooks cannot be removed, so install once and gate on _ACTIVE.
    global _HOOKED
    if not _HOOKED:
        sys.addaudithook(_audit)
        _HOOKED = True


# ---------------------------------------------------------- script wrapper


def run_with_manifest(main: Callable[[], int], script_file: str) -> int:
    """Run a validation script's `main`, with a manifest if `--manifest` was
    passed. Without it this is exactly `main()`."""
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--manifest", action="store_true")
    flags, _ = probe.parse_known_args()
    if not flags.manifest:
        return main()

    script = Path(script_file).stem
    manifest = DataManifest()
    with manifest.tracking():
        status = main()
    target = manifest.write(MANIFEST_DIR / f"{script}.json", script)
    print(f"\n{'=' * 78}\nDATA MANIFEST\n{'=' * 78}")
    print(manifest.format())
    print(f"  written to {_display(target)}")
    return status
