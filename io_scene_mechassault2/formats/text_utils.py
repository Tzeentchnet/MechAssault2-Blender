"""Path normalization, tolerant XML parser, MAXPort INI parser, and .road parser."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional, Tuple


def normalize_path(path: str) -> str:
    """Normalize archive/asset paths: '/' separators, collapse '.' and '..', no leading slash."""
    parts: List[str] = []
    for raw in path.replace("\\", "/").split("/"):
        if not raw or raw == ".":
            continue
        if raw == "..":
            if parts:
                parts.pop()
        else:
            parts.append(raw)
    return "/".join(parts)


def folder_of(path: str) -> str:
    norm = normalize_path(path)
    idx = norm.rfind("/")
    return "" if idx < 0 else norm[:idx]


def file_name(path: str) -> str:
    norm = normalize_path(path)
    idx = norm.rfind("/")
    return norm if idx < 0 else norm[idx + 1:]


def file_stem(path: str) -> str:
    name = file_name(path)
    dot = name.rfind(".")
    return name if dot < 0 else name[:dot]


def extension(path: str) -> str:
    """Lower-case extension without dot, stripping any '{subentry}' suffix."""
    name = file_name(path)
    brace = name.find("{")
    if brace >= 0:
        name = name[:brace]
    dot = name.rfind(".")
    return "" if dot < 0 else name[dot + 1:].lower()


def resolve_relative(base_file: str, relative: str) -> str:
    """Resolve `relative` against `base_file`'s folder (leading slash = archive root)."""
    rel = relative.strip()
    if not rel:
        return ""
    if rel[0] in ("/", "\\"):
        return normalize_path(rel)
    base_dir = folder_of(base_file)
    return normalize_path(f"{base_dir}/{rel}" if base_dir else rel)


_FLOAT_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_floats(text: str, max_count: int = 16) -> List[float]:
    if not text:
        return []
    out: List[float] = []
    for m in _FLOAT_RE.finditer(text):
        out.append(float(m.group(0)))
        if len(out) >= max_count:
            break
    return out


def parse_bool(text: str, fallback: bool = False) -> bool:
    t = text.strip().lower()
    if t in ("true", "yes", "1", "on"):
        return True
    if t in ("false", "no", "0", "off"):
        return False
    return fallback


def parse_color_255(text: str, fallback: Tuple[float, float, float] = (1.0, 1.0, 1.0)) -> Tuple[float, float, float]:
    vals = parse_floats(text, 3)
    if len(vals) == 3:
        return (
            min(1.0, max(0.0, vals[0] / 255.0)),
            min(1.0, max(0.0, vals[1] / 255.0)),
            min(1.0, max(0.0, vals[2] / 255.0)),
        )
    return fallback


# ---------------------------------------------------------------------------
# Tolerant XML parser (matching editor/models/src/text.cpp)
# ---------------------------------------------------------------------------

def _decode_entities(s: str) -> str:
    if "&" not in s:
        return s
    out: List[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "&":
            semi = s.find(";", i + 1, min(n, i + 10))
            if semi > i:
                ent = s[i + 1:semi]
                ch = None
                if ent == "amp":
                    ch = "&"
                elif ent == "lt":
                    ch = "<"
                elif ent == "gt":
                    ch = ">"
                elif ent == "quot":
                    ch = '"'
                elif ent == "apos":
                    ch = "'"
                elif ent.startswith("#x"):
                    try:
                        ch = chr(int(ent[2:], 16))
                    except ValueError:
                        ch = None
                elif ent.startswith("#"):
                    try:
                        ch = chr(int(ent[1:], 10))
                    except ValueError:
                        ch = None
                if ch is not None:
                    out.append(ch)
                    i = semi + 1
                    continue
        out.append(s[i])
        i += 1
    return "".join(out)


@dataclass
class XmlNode:
    tag: str = ""
    attrs: List[Tuple[str, str]] = field(default_factory=list)
    children: List["XmlNode"] = field(default_factory=list)
    _attr_map: Optional[Dict[str, str]] = field(default=None, repr=False, compare=False)

    def _ensure_map(self) -> Dict[str, str]:
        if self._attr_map is None:
            self._attr_map = {k.lower(): v for k, v in self.attrs}
        return self._attr_map

    def attr(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self._ensure_map().get(name.lower(), default)

    def attr_or(self, name: str, default: str = "") -> str:
        val = self.attr(name)
        return default if val is None else val

    def child(self, tag: str) -> Optional["XmlNode"]:
        tl = tag.lower()
        for c in self.children:
            if c.tag.lower() == tl:
                return c
        return None

    def children_named(self, tag: str) -> List["XmlNode"]:
        tl = tag.lower()
        return [c for c in self.children if c.tag.lower() == tl]


def parse_xml(raw_text: bytes | str) -> XmlNode:
    """Parse MA2 XML (.MGMODEL / .mgparticle / .anm) tolerating trailing NULs & unescaped quirks."""
    if isinstance(raw_text, (bytes, bytearray)):
        text = raw_text.rstrip(b"\x00").decode("latin-1", errors="replace")
    else:
        text = raw_text.rstrip("\x00")
    n = len(text)
    pos = 0

    def skip_ws() -> None:
        nonlocal pos
        while pos < n and text[pos].isspace():
            pos += 1

    def skip_misc() -> None:
        nonlocal pos
        while pos < n:
            skip_ws()
            if text.startswith("<?", pos):
                end = text.find("?>", pos + 2)
                pos = n if end < 0 else end + 2
            elif text.startswith("<!--", pos):
                end = text.find("-->", pos + 4)
                pos = n if end < 0 else end + 3
            elif text.startswith("<!", pos):
                end = text.find(">", pos + 2)
                pos = n if end < 0 else end + 1
            else:
                break

    def read_name() -> str:
        nonlocal pos
        start = pos
        while pos < n and not text[pos].isspace() and text[pos] not in (">", "/", "="):
            pos += 1
        return text[start:pos]

    def parse_element(depth: int) -> XmlNode:
        nonlocal pos
        if depth > 256:
            raise ValueError("XML nesting too deep")
        if pos >= n or text[pos] != "<":
            raise ValueError(f"XML: expected '<' at {pos}")
        pos += 1
        tag = read_name()
        if not tag:
            raise ValueError(f"XML: empty tag name at {pos}")
        node = XmlNode(tag=tag)
        while True:
            skip_ws()
            if pos >= n:
                raise ValueError(f"XML: unterminated tag <{tag}>")
            if text[pos] == "/":
                pos += 1
                if pos < n and text[pos] == ">":
                    pos += 1
                return node
            if text[pos] == ">":
                pos += 1
                break
            key = read_name()
            skip_ws()
            val = ""
            if pos < n and text[pos] == "=":
                pos += 1
                skip_ws()
                if pos < n and text[pos] in ('"', "'"):
                    qc = text[pos]
                    pos += 1
                    start = pos
                    end_q = text.find(qc, start)
                    if end_q < 0:
                        val = text[start:]
                        pos = n
                    else:
                        val = text[start:end_q]
                        pos = end_q + 1
                else:
                    start = pos
                    while pos < n and not text[pos].isspace() and text[pos] not in (">", "/"):
                        pos += 1
                    val = text[start:pos]
            if not key:
                pos += 1
                continue
            node.attrs.append((key, _decode_entities(val)))

        while True:
            lt = text.find("<", pos)
            if lt < 0:
                raise ValueError(f"XML: missing </{tag}>")
            pos = lt
            if text.startswith("</", pos):
                pos += 2
                read_name()
                gt = text.find(">", pos)
                pos = n if gt < 0 else gt + 1
                return node
            if text.startswith(("<!--", "<?", "<!"), pos):
                skip_misc()
                continue
            node.children.append(parse_element(depth + 1))

    skip_misc()
    return parse_element(0)


# ---------------------------------------------------------------------------
# MAXPort INI parser (.NODE / .mesh / .mat / .road / Multiplay/*.ini)
# ---------------------------------------------------------------------------

def _unquote(s: str) -> str:
    t = s.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1]
    if t.startswith('"'):
        return t[1:]
    return t


@dataclass
class IniSection:
    name: str
    values: List[Tuple[str, str]] = field(default_factory=list)

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        kl = key.lower()
        for k, v in self.values:
            if k.lower() == kl:
                return v
        return default

    def get_all(self, key: str) -> List[str]:
        kl = key.lower()
        return [v for k, v in self.values if k.lower() == kl]


def parse_ini(raw_text: bytes | str) -> List[IniSection]:
    """Parse MAXPort INI keeping repeated keys and multi-line continuation values."""
    if isinstance(raw_text, (bytes, bytearray)):
        text = raw_text.rstrip(b"\x00").decode("latin-1", errors="replace")
    else:
        text = raw_text.rstrip("\x00")
    sections: List[IniSection] = []
    cur: Optional[IniSection] = None
    for raw_line in text.splitlines():
        # Strip ';' comments outside quotes
        in_quote = False
        line = raw_line
        for i, ch in enumerate(raw_line):
            if ch == '"':
                in_quote = not in_quote
            elif ch == ";" and not in_quote:
                line = raw_line[:i]
                break
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            close = line.find("]")
            sec_name = line[1:close].strip() if close >= 0 else line[1:].strip()
            cur = IniSection(name=sec_name)
            sections.append(cur)
            continue
        eq = line.find("=")
        if cur is None:
            cur = IniSection(name="")
            sections.append(cur)
        if eq < 0:
            if cur.values:
                k, v = cur.values[-1]
                cur.values[-1] = (k, f"{v} {line}" if v else line)
            continue
        key = line[:eq].strip()
        val = _unquote(line[eq + 1:])
        cur.values.append((key, val))
    return sections


def parse_road_definition(raw_text: bytes | str, road_path: str) -> Tuple[str, Tuple[float, float], Tuple[float, float]]:
    """Parse a .road INI file -> (resolved_model_path, (x0, x1), (z0, z1))."""
    secs = parse_ini(raw_text)
    model_path = ""
    p1 = [13.0, 0.0, 0.0]
    p2 = [-13.0, 0.0, -30.0]
    for s in secs:
        for k, v in s.values:
            kl = k.lower()
            if kl == "model" and v:
                model_path = resolve_relative(road_path, v)
            elif kl == "point1":
                vals = parse_floats(v, 3)
                if len(vals) == 3:
                    p1 = vals
            elif kl == "point2":
                vals = parse_floats(v, 3)
                if len(vals) == 3:
                    p2 = vals
    return (
        model_path,
        (min(p1[0], p2[0]), max(p1[0], p2[0])),
        (min(p1[2], p2[2]), max(p1[2], p2[2])),
    )
