"""Pure-Python Xbox XDVDFS (.iso / .xiso.iso) reader for zero-copy streaming."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import struct
from typing import BinaryIO, Dict, List, Optional, Tuple

from .text_utils import normalize_path

SECTOR_SIZE = 2048
XDVDFS_MAGIC = b"MICROSOFT*XBOX*MEDIA"
XDVDFS_HEADER_OFFSET = 32 * SECTOR_SIZE  # 0x10000
# Candidate game-partition base offsets:
# 0x0        = trimmed XISO (.xiso.iso)
# 0x18300000 = standard Redump XGD1 video+game partition split
# 0xFD90000  = XGD2 video partition split
# 0x2080000  = XGD3 video partition split
KNOWN_PARTITION_OFFSETS: Tuple[int, ...] = (0x0, 0x18300000, 0xFD90000, 0x2080000)


@dataclass(frozen=True)
class XisoEntry:
    name: str
    path: str
    offset: int  # Absolute byte offset in the ISO file
    size: int
    is_dir: bool = False


def detect_xiso_partition_offset(fp: BinaryIO, file_size: int) -> int:
    """Detect the XDVDFS game partition offset inside an open ISO stream."""
    for base in KNOWN_PARTITION_OFFSETS:
        hdr = base + XDVDFS_HEADER_OFFSET
        if hdr + SECTOR_SIZE > file_size:
            continue
        fp.seek(hdr)
        sec = fp.read(SECTOR_SIZE)
        if len(sec) == SECTOR_SIZE and sec[:20] == XDVDFS_MAGIC and sec[0x7EC:0x800] == XDVDFS_MAGIC:
            return base
    raise ValueError("Not a valid Xbox XDVDFS (.iso / .xiso.iso) image")


def is_xiso_file(path: str | os.PathLike[str]) -> bool:
    """Return True if `path` is an existing file whose header matches Xbox XDVDFS."""
    p = Path(path)
    if not p.is_file():
        return False
    low = p.name.lower()
    if not (low.endswith(".iso") or low.endswith(".xiso")):
        return False
    try:
        sz = p.stat().st_size
        with open(p, "rb") as fp:
            detect_xiso_partition_offset(fp, sz)
        return True
    except (OSError, ValueError):
        return False


class XisoArchive:
    """Read-only Xbox XDVDFS (.iso / .xiso.iso) filesystem walker and file reader."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.name = self.path.name
        self._fp = open(self.path, "rb")
        try:
            self._fp.seek(0, os.SEEK_END)
            self.file_size = self._fp.tell()
            self.partition_offset = detect_xiso_partition_offset(self._fp, self.file_size)

            self._fp.seek(self.partition_offset + XDVDFS_HEADER_OFFSET + 20)
            root_sec, root_size = struct.unpack("<II", self._fp.read(8))

            self.entries: List[XisoEntry] = []
            self._index: Dict[str, int] = {}

            self._walk_dir(root_sec, root_size, "")
        except Exception:
            self.close()
            raise

    def _walk_dir(self, start_sector: int, dir_size: int, prefix: str, depth: int = 0) -> None:
        if dir_size <= 0 or depth > 64:
            return
        dir_off = self.partition_offset + start_sector * SECTOR_SIZE
        if dir_off < 0 or dir_off + dir_size > self.file_size:
            raise ValueError(f"XDVDFS directory table out of bounds at sector {start_sector}")
        self._fp.seek(dir_off)
        buf = self._fp.read(dir_size)
        if len(buf) < dir_size:
            raise IOError(f"Short XDVDFS directory read in {self.name}")

        subdirs: List[Tuple[int, int, str]] = []
        stack: List[int] = [0]
        visited: set[int] = set()

        while stack:
            off = stack.pop()
            if off in visited or off < 0 or off + 14 > dir_size:
                continue
            visited.add(off)

            left_off, right_off, entry_sec, entry_size, attr, name_len = struct.unpack_from("<HHIIBB", buf, off)
            if left_off != 0 and left_off != 0xFFFF:
                stack.append(left_off * 4)
            if right_off != 0 and right_off != 0xFFFF:
                stack.append(right_off * 4)

            if name_len == 0 or off + 14 + name_len > dir_size:
                continue
            raw_name = buf[off + 14:off + 14 + name_len]
            name = raw_name.decode("latin-1", errors="replace")
            if not name or name in (".", ".."):
                continue

            full_path = normalize_path(f"{prefix}/{name}" if prefix else name)
            abs_off = self.partition_offset + entry_sec * SECTOR_SIZE
            is_dir = bool(attr & 0x10)

            idx = len(self.entries)
            self.entries.append(
                XisoEntry(
                    name=name,
                    path=full_path,
                    offset=abs_off,
                    size=entry_size,
                    is_dir=is_dir,
                )
            )
            self._index.setdefault(full_path.lower(), idx)

            if is_dir:
                subdirs.append((entry_sec, entry_size, full_path))

        for s_sec, s_size, s_path in subdirs:
            self._walk_dir(s_sec, s_size, s_path, depth=depth + 1)

    def close(self) -> None:
        if getattr(self, "_fp", None) is not None and not self._fp.closed:
            self._fp.close()

    def __enter__(self) -> "XisoArchive":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def exists(self, path: str) -> bool:
        idx = self._index.get(normalize_path(path).lower())
        return idx is not None and not self.entries[idx].is_dir

    def find_entry(self, path: str) -> Optional[XisoEntry]:
        idx = self._index.get(normalize_path(path).lower())
        return None if idx is None else self.entries[idx]

    def read(self, path: str) -> bytes:
        entry = self.find_entry(path)
        if entry is None or entry.is_dir:
            raise FileNotFoundError(f"Not found in XISO {self.name}: {path}")
        if entry.offset + entry.size > self.file_size:
            raise IOError(f"XISO entry out of bounds: {entry.path}")
        self._fp.seek(entry.offset)
        data = self._fp.read(entry.size)
        if len(data) != entry.size:
            raise IOError(f"Short XISO read for {entry.path}")
        return data

    def mgf_entries(self) -> List[XisoEntry]:
        return [e for e in self.entries if not e.is_dir and e.name.lower().endswith(".mgf")]
