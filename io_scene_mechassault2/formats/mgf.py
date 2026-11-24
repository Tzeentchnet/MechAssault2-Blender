"""MechAssault 1 & 2 MGF archive reader, ArchiveSet, and unified AssetSource."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
from pathlib import Path
import struct
from typing import BinaryIO, Dict, List, Optional, Sequence, Tuple
import zlib

from .text_utils import extension, file_name, file_stem, folder_of, normalize_path
from .xiso import XisoArchive, is_xiso_file

MGF_SIGNATURE = b"mgf "
MGF_COMPRESSED_MAGIC = 0xC5735A0F


@dataclass(frozen=True)
class MgfEntry:
    path: str
    offset: int
    length: int


class MGFArchive:
    """Streaming MGF v3 (MA1) & v4 (MA2) reader supporting plain, zlib-blocked, and XISO-sliced archives."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        base_offset: int = 0,
        slice_size: Optional[int] = None,
        virtual_name: Optional[str] = None,
        fp: Optional[BinaryIO] = None,
    ):
        self.path = Path(path)
        self.name = virtual_name or self.path.name
        self._base_offset = int(base_offset)
        self._owns_fp = fp is None
        self._fp = fp if fp is not None else open(self.path, "rb")
        try:
            self._parse_header_and_dir(slice_size)
        except Exception:
            self.close()
            raise

    def _parse_header_and_dir(self, slice_size: Optional[int]) -> None:
        if slice_size is not None:
            self.file_size = int(slice_size)
        else:
            self._fp.seek(0, os.SEEK_END)
            self.file_size = max(0, self._fp.tell() - self._base_offset)
        if self.file_size < 44:
            raise ValueError(f"MGF file too small: {self.name}")

        self._fp.seek(self._base_offset)
        head = self._fp.read(32)
        words = struct.unpack_from("<8I", head, 0)

        self._compressed = False
        self._block_size = 0
        self._chunks: List[Tuple[int, int, int]] = []
        self._block_cache: OrderedDict[int, bytes] = OrderedDict()

        if head[:4] == MGF_SIGNATURE:
            self.logical_size = self.file_size
        elif words[1] == MGF_COMPRESSED_MAGIC:
            count = words[2]
            self.logical_size = words[3]
            self._block_size = words[5]
            if (
                count == 0
                or count > 1_000_000
                or self._block_size == 0
                or self._block_size > (64 << 20)
                or count * self._block_size < self.logical_size
            ):
                raise ValueError(f"Invalid compressed MGF header: {self.path}")
            self._fp.seek(self._base_offset + 32)
            chunk_bytes = self._fp.read(count * 12)
            if len(chunk_bytes) < count * 12:
                raise ValueError(f"Truncated MGF block table: {self.path}")
            self._chunks = [struct.unpack_from("<III", chunk_bytes, i * 12) for i in range(count)]
            self._compressed = True
        else:
            raise ValueError(f"Not an MGF archive: {self.name}")

        hdr_bytes = self._read_logical(0, 44)
        hdr = struct.unpack("<11I", hdr_bytes)
        if hdr[0] != 0x2066676D:  # 'mgf '
            raise ValueError(f"Invalid MGF magic in header: {self.path}")
        self.version = hdr_bytes[4]
        if self.version not in (3, 4):
            raise ValueError(f"Unsupported MGF version {self.version}: {self.path}")

        nf, lf, of, nd, ld, od, ls, os_off = hdr[3:11]
        rec_stride = 28 if self.version == 3 else 32
        if lf != nf * rec_stride or ld != nd * 24 or nf > 1_000_000 or nd > 1_000_000 or ls > (64 << 20):
            raise ValueError(f"Invalid MGF directory table sizes: {self.path}")

        records_raw = self._read_logical(of, lf) if nf else b""
        rows_raw = self._read_logical(od, ld) if nd else b""
        strings_raw = self._read_logical(os_off, ls) if ls else b""

        records: List[Tuple[int, int]] = []
        if self.version == 4:
            for i in range(nf):
                rec = struct.unpack_from("<8I", records_raw, i * 32)
                records.append((rec[6], rec[3]))  # (offset, length)
        else:
            for i in range(nf):
                rec = struct.unpack_from("<7I", records_raw, i * 28)
                records.append((rec[6], rec[5]))  # (offset, length)

        rows = [struct.unpack_from("<6i", rows_raw, i * 24) for i in range(nd)]

        def row_name(r_idx: int) -> str:
            off = rows[r_idx][4] & 0xFFFFFFFF
            if off >= ls:
                return ""
            end = strings_raw.find(b"\x00", off)
            if end < 0:
                end = ls
            return strings_raw[off:end].decode("latin-1", errors="replace")

        self.entries: List[MgfEntry] = []
        self._index: Dict[str, int] = {}

        for r_idx in range(nd):
            fi = rows[r_idx][5]
            if fi < 0 or fi >= nf:
                continue
            parts: List[str] = []
            cur = r_idx
            guard = 0
            ok = True
            while cur >= 0:
                if cur >= nd or guard > 256:
                    ok = False
                    break
                guard += 1
                nm = row_name(cur)
                if nm:
                    parts.append(nm)
                cur = rows[cur][1]
            if not ok or not parts:
                continue
            full = normalize_path("/".join(reversed(parts)))
            offset, length = records[fi]
            idx = len(self.entries)
            self.entries.append(MgfEntry(path=full, offset=offset, length=length))
            self._index.setdefault(full.lower(), idx)

    def close(self) -> None:
        if getattr(self, "_owns_fp", False) and self._fp is not None and not self._fp.closed:
            self._fp.close()

    def __enter__(self) -> "MGFArchive":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _raw_read(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.file_size:
            raise IOError(f"MGF read out of bounds ({offset}+{size} > {self.file_size}) in {self.name}")
        self._fp.seek(self._base_offset + offset)
        data = self._fp.read(size)
        if len(data) != size:
            raise IOError(f"Short MGF read in {self.name}")
        return data

    def _get_block(self, number: int) -> bytes:
        if number in self._block_cache:
            self._block_cache.move_to_end(number)
            return self._block_cache[number]
        if number < 0 or number >= len(self._chunks):
            raise IOError(f"MGF block {number} out of range in {self.name}")
        start, length, _checksum = self._chunks[number]
        packed = self._raw_read(start, length)
        block = zlib.decompress(packed)
        self._block_cache[number] = block
        if len(self._block_cache) > 32:
            self._block_cache.popitem(last=False)
        return block

    def _read_logical(self, address: int, size: int) -> bytes:
        if address < 0 or size < 0 or address + size > self.logical_size:
            raise IOError(f"MGF logical read out of bounds ({address}+{size} > {self.logical_size}) in {self.name}")
        if size == 0:
            return b""
        if not self._compressed:
            return self._raw_read(address, size)
        parts: List[bytes] = []
        remaining = size
        cur = address
        while remaining > 0:
            number, offset = divmod(cur, self._block_size)
            block = self._get_block(number)
            take = min(len(block) - offset, remaining)
            if take <= 0:
                raise IOError(f"Short decompressed MGF block {number} in {self.name}")
            parts.append(block[offset:offset + take])
            cur += take
            remaining -= take
        return b"".join(parts)

    def exists(self, path: str) -> bool:
        return normalize_path(path).lower() in self._index

    def canonical_path(self, path: str) -> str:
        idx = self._index.get(normalize_path(path).lower())
        return "" if idx is None else self.entries[idx].path

    def read(self, path: str) -> bytes:
        idx = self._index.get(normalize_path(path).lower())
        if idx is None:
            raise FileNotFoundError(f"Not found in {self.name}: {path}")
        entry = self.entries[idx]
        return self._read_logical(entry.offset, entry.length)

    def list_files(self, substring: str = "") -> List[str]:
        needle = substring.lower()
        if not needle:
            return sorted(e.path for e in self.entries)
        return sorted(e.path for e in self.entries if needle in e.path.lower())


class ArchiveSet:
    """Prioritized collection of MGF archives (first archive added wins)."""

    def __init__(self) -> None:
        self.archives: List[MGFArchive] = []
        self._global: Dict[str, Tuple[int, int]] = {}  # lower_path -> (archive_idx, entry_idx)
        self._owned_fps: List[BinaryIO] = []

    def own_fp(self, fp: BinaryIO) -> None:
        self._owned_fps.append(fp)

    def add_archive(self, ar: MGFArchive) -> MGFArchive:
        ai = len(self.archives)
        self.archives.append(ar)
        for key, ei in ar._index.items():
            self._global.setdefault(key, (ai, ei))
        return ar

    def add(self, path: str | os.PathLike[str]) -> MGFArchive:
        return self.add_archive(MGFArchive(path))

    def close(self) -> None:
        for ar in self.archives:
            ar.close()
        self.archives.clear()
        self._global.clear()
        for fp in self._owned_fps:
            try:
                if not fp.closed:
                    fp.close()
            except Exception:
                pass
        self._owned_fps.clear()

    def find(self, path: str) -> int:
        hit = self._global.get(normalize_path(path).lower())
        return -1 if hit is None else hit[0]

    def exists(self, path: str) -> bool:
        return normalize_path(path).lower() in self._global

    def canonical_path(self, path: str) -> str:
        hit = self._global.get(normalize_path(path).lower())
        if hit is None:
            return ""
        ai, ei = hit
        return self.archives[ai].entries[ei].path

    def archive_name(self, path: str) -> str:
        hit = self._global.get(normalize_path(path).lower())
        if hit is None:
            return ""
        return self.archives[hit[0]].name

    def read(self, path: str) -> bytes:
        hit = self._global.get(normalize_path(path).lower())
        if hit is None:
            raise FileNotFoundError(f"Not found in ArchiveSet: {path}")
        ai, ei = hit
        entry = self.archives[ai].entries[ei]
        return self.archives[ai]._read_logical(entry.offset, entry.length)

    def list_all(self, substring: str = "") -> List[str]:
        needle = substring.lower()
        out: List[str] = []
        for k, (ai, ei) in self._global.items():
            if needle and needle not in k:
                continue
            out.append(self.archives[ai].entries[ei].path)
        out.sort()
        return out


class AssetSource:
    """Unified asset lookup across loose files on disk and an ArchiveSet."""

    def __init__(self, archive_set: Optional[ArchiveSet] = None, disk_root: Optional[Path] = None) -> None:
        self.archives = archive_set
        self.disk_root = Path(disk_root) if disk_root else None
        self._disk_index: Optional[Dict[str, Path]] = None

    def _ensure_disk_index(self) -> Dict[str, Path]:
        if self._disk_index is not None:
            return self._disk_index
        idx: Dict[str, Path] = {}
        if self.disk_root and self.disk_root.is_dir():
            for root, _dirs, files in os.walk(self.disk_root):
                rel_dir = Path(root).relative_to(self.disk_root)
                for fn in files:
                    rel = normalize_path((rel_dir / fn).as_posix())
                    idx[rel.lower()] = Path(root) / fn
        self._disk_index = idx
        return idx

    def _find_disk_path(self, path: str) -> Optional[Path]:
        if not self.disk_root:
            return None
        norm = normalize_path(path)
        direct = self.disk_root / norm
        if direct.is_file():
            return direct
        return self._ensure_disk_index().get(norm.lower())

    def exists(self, path: str) -> bool:
        if not path:
            return False
        if self._find_disk_path(path) is not None:
            return True
        return bool(self.archives and self.archives.exists(path))

    def canonical_path(self, path: str) -> str:
        norm = normalize_path(path)
        dp = self._find_disk_path(norm)
        if dp is not None and self.disk_root:
            try:
                return normalize_path(dp.relative_to(self.disk_root).as_posix())
            except ValueError:
                return norm
        if self.archives and self.archives.exists(norm):
            return self.archives.canonical_path(norm)
        return ""

    def read(self, path: str) -> bytes:
        dp = self._find_disk_path(path)
        if dp is not None:
            return dp.read_bytes()
        if self.archives and self.archives.exists(path):
            return self.archives.read(path)
        raise FileNotFoundError(f"Asset not found: {path}")

    def list_all(self, substring: str = "") -> List[str]:
        needle = substring.lower()
        found: Dict[str, str] = {}
        if self.disk_root:
            for k, p in self._ensure_disk_index().items():
                if not needle or needle in k:
                    found[k] = normalize_path(p.relative_to(self.disk_root).as_posix())
        if self.archives:
            for p in self.archives.list_all(substring):
                found.setdefault(p.lower(), p)
        return sorted(found.values())


def model_path_fallbacks(norm: str) -> List[str]:
    """The `_low` and .NODE <-> .MGMODEL stand-ins tried, in order, when model path `norm` is absent."""
    folder = folder_of(norm)
    stem = file_stem(norm)
    ext = extension(norm)
    prefix = f"{folder}/" if folder else ""

    candidates: List[str] = []
    if ext:
        candidates.append(f"{prefix}{stem}_low.{ext}")
    other_ext = "node" if ext == "mgmodel" else ("mgmodel" if ext == "node" else "")
    if other_ext:
        candidates.append(f"{prefix}{stem}.{other_ext}")
        candidates.append(f"{prefix}{stem}_low.{other_ext}")
        # Preserve original uppercase variant too just in case
        other_up = other_ext.upper()
        candidates.append(f"{prefix}{stem}.{other_up}")
        candidates.append(f"{prefix}{stem}_low.{other_up}")
    return candidates


def resolve_model_path(source: AssetSource, path: str) -> str:
    """Resolve a model path including _low and .NODE <-> .MGMODEL fallbacks."""
    norm = normalize_path(path)
    if not norm:
        return ""
    if source.exists(norm):
        return source.canonical_path(norm)

    for c in model_path_fallbacks(norm):
        if source.exists(c):
            return source.canonical_path(c)
    return ""


def open_game_archives(
    game_files_dir: str | os.PathLike[str],
    level_mgf: Optional[str | os.PathLike[str]] = None,
    iso_mgf_entries: Optional[Sequence[Tuple[str, int, int]]] = None,
) -> ArchiveSet:
    """Open an ArchiveSet from a directory, .mgf, or Xbox .iso/.xiso.iso image.

    Prioritizes level_mgf (if given), then Base*.mgf, common.mgf / Templates.mgf, and remaining *.mgf.
    """
    root = Path(game_files_dir)
    aset = ArchiveSet()

    if root.is_file():
        if is_xiso_file(root):
            if iso_mgf_entries is None:
                with XisoArchive(root) as xiso:
                    slices = [(e.name, e.offset, e.size) for e in xiso.mgf_entries()]
            else:
                slices = list(iso_mgf_entries)

            slices.sort(key=lambda s: s[0].lower())
            lvl_name = Path(str(level_mgf)).name.lower() if level_mgf else ""
            chosen: List[Tuple[str, int, int]] = []
            used: set[int] = set()
            if lvl_name:
                for i, (nm, off, sz) in enumerate(slices):
                    if nm.lower() == lvl_name:
                        chosen.append((nm, off, sz))
                        used.add(i)
                        break

            base_s = [(i, s) for i, s in enumerate(slices) if i not in used and s[0].lower().startswith("base")]
            prio_s = [
                (i, s) for i, s in enumerate(slices)
                if i not in used and s[0].lower() in ("common.mgf", "templates.mgf")
            ]
            rest_s = [
                (i, s) for i, s in enumerate(slices)
                if i not in used
                and not s[0].lower().startswith("base")
                and s[0].lower() not in ("common.mgf", "templates.mgf")
            ]
            fp = open(root, "rb")
            aset.own_fp(fp)
            ordered = chosen + [s for _i, s in base_s + prio_s + rest_s]
            try:
                for nm, off, sz in ordered:
                    aset.add_archive(MGFArchive(root, base_offset=off, slice_size=sz, virtual_name=nm, fp=fp))
            except Exception:
                aset.close()
                raise
            return aset

        if root.suffix.lower() == ".mgf":
            aset.add(root)
        return aset

    opened: set[Path] = set()

    def try_add(p: Path) -> None:
        rp = p.resolve()
        if rp in opened or not rp.is_file():
            return
        opened.add(rp)
        aset.add(rp)

    if level_mgf:
        lp = Path(level_mgf)
        if not lp.is_absolute():
            lp = root / lp
        try_add(lp)

    if root.is_dir():
        # Base / common first after the active level archive
        all_mgfs = sorted(root.glob("*.mgf"), key=lambda p: p.name.lower())
        priority = [p for p in all_mgfs if p.name.lower() in ("common.mgf", "templates.mgf")]
        base_mgfs = [p for p in all_mgfs if p.name.lower().startswith("base")]
        rest = [p for p in all_mgfs if p not in priority and p not in base_mgfs]
        for p in base_mgfs + priority + rest:
            try_add(p)
    return aset


def infer_game_files_dir(file_path: str | os.PathLike[str]) -> Optional[Path]:
    """Walk up from a loose file to locate a directory containing common.mgf or game_files/common.mgf."""
    cur = Path(file_path).resolve()
    if cur.is_file():
        cur = cur.parent
    for _ in range(8):
        if (cur / "common.mgf").is_file():
            return cur
        if (cur / "game_files" / "common.mgf").is_file():
            return cur / "game_files"
        if cur.parent == cur:
            break
        cur = cur.parent
    return None
