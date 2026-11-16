"""Parse VCS/Verilator-style RTL filelists (.f) with lazy library directory support."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

MODULE_DECL = re.compile(r"\bmodule\s+(?:automatic\s+|static\s+)?([A-Za-z_][A-Za-z0-9_$]*)\b")
INST_CANDIDATE = re.compile(
    r"(?m)^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:#\s*\(|[A-Za-z_][A-Za-z0-9_$]*\s*\()"
)
SV_KEYWORDS = {
    "module", "endmodule", "function", "task", "if", "else", "for", "while",
    "case", "casex", "casez", "unique", "priority", "always", "always_ff",
    "always_comb", "always_latch", "initial", "assign", "begin", "end",
    "generate", "endgenerate", "property", "sequence", "assert", "assume", "cover",
}


@dataclass(frozen=True)
class Filelist:
    files: list[Path]
    include_dirs: list[Path]
    defines: list[str]
    library_dirs: list[Path]
    libexts: list[str]
    filelists: list[Path]


def _unique(items: list) -> list:
    seen: set[str] = set()
    out = []
    for item in items:
        key = str(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _resolve(token: str, base: Path) -> Path:
    expanded = Path(token).expanduser()
    return (expanded if expanded.is_absolute() else base / expanded).resolve()


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//.*", "", text)


def resolve_library_files(files: list[Path], library_dirs: list[Path], libexts: list[str]) -> list[Path]:
    """Lazily resolve instantiated module files from -y library directories."""
    if not library_dirs:
        return files
    exts = libexts or [".sv", ".v"]
    resolved = list(files)
    defined: set[str] = set()
    queue = list(files)
    scanned: set[Path] = set()

    while queue:
        src = queue.pop(0)
        if src in scanned or not src.is_file():
            continue
        scanned.add(src)
        clean = _strip_comments(src.read_text(errors="replace"))
        defined.update(MODULE_DECL.findall(clean))
        for cand in INST_CANDIDATE.findall(clean):
            if cand in SV_KEYWORDS or cand in defined:
                continue
            for libdir in library_dirs:
                found = None
                for ext in exts:
                    candidate_path = (libdir / f"{cand}{ext}").resolve()
                    if candidate_path.is_file():
                        found = candidate_path
                        break
                if found is not None:
                    if found not in resolved:
                        resolved.append(found)
                        queue.append(found)
                    break
    return _unique(resolved)


@dataclass
class Parser:
    files: list[Path] = field(default_factory=list)
    include_dirs: list[Path] = field(default_factory=list)
    defines: list[str] = field(default_factory=list)
    library_dirs: list[Path] = field(default_factory=list)
    libexts: list[str] = field(default_factory=list)
    filelists: list[Path] = field(default_factory=list)
    visited: set[tuple[Path, Path]] = field(default_factory=set)
    cwd: Path | None = None

    def parse(self, root: Path) -> Filelist:
        self.cwd = root.resolve().parent
        self._read(root.resolve(), self.cwd)
        for directory in _unique(self.library_dirs):
            if not directory.is_dir():
                raise SystemExit(f"Missing library directory: {directory}")
        files = _unique(self.files)
        missing = [str(p) for p in files if not p.is_file()]
        if missing:
            raise SystemExit("Missing RTL files:\n" + "\n".join(missing))
        if not files:
            raise SystemExit(f"No RTL files found in {root}")
        libexts = _unique(self.libexts) or [".sv", ".v"]
        return Filelist(
            files=files,
            include_dirs=_unique(self.include_dirs),
            defines=_unique(self.defines),
            library_dirs=_unique(self.library_dirs),
            libexts=libexts,
            filelists=_unique(self.filelists),
        )

    def _read(self, path: Path, content_base: Path) -> None:
        visit = (path, content_base.resolve())
        if visit in self.visited:
            return
        if not path.is_file():
            raise SystemExit(f"Missing filelist: {path}")
        self.visited.add(visit)
        self.filelists.append(path)

        tokens: list[str] = []
        for raw in path.read_text(errors="replace").splitlines():
            line = re.sub(r"//.*$", "", raw).strip()
            if line and not line.startswith("#"):
                tokens.extend(shlex.split(line, comments=True))

        i = 0
        while i < len(tokens):
            tok = tokens[i]
            base = content_base
            if tok in {"-f", "-F", "-v", "-y", "-I", "-D"}:
                if i + 1 >= len(tokens):
                    raise SystemExit(f"Missing argument after {tok} in {path}")
                val = tokens[i + 1]
                i += 2
                if tok == "-f":
                    nested = _resolve(val, self.cwd or base)
                    self._read(nested, self.cwd or base)
                elif tok == "-F":
                    nested = _resolve(val, base)
                    self._read(nested, nested.parent)
                elif tok == "-v":
                    self.files.append(_resolve(val, base))
                elif tok == "-y":
                    self.library_dirs.append(_resolve(val, base))
                elif tok == "-I":
                    self.include_dirs.append(_resolve(val, base))
                elif tok == "-D":
                    self._add_defines(val)
                continue
            if tok.startswith("+incdir+"):
                self.include_dirs.extend(
                    _resolve(v, base) for v in tok[len("+incdir+"):].split("+") if v
                )
            elif tok.startswith("+define+"):
                self._add_defines(tok[len("+define+"):])
            elif tok.startswith("+libext+"):
                for ext in tok[len("+libext+"):].split("+"):
                    if ext:
                        self.libexts.append(ext if ext.startswith(".") else f".{ext}")
            elif tok.startswith("-I") and len(tok) > 2:
                self.include_dirs.append(_resolve(tok[2:], base))
            elif tok.startswith("-D") and len(tok) > 2:
                self._add_defines(tok[2:])
            elif tok in {"-sv", "-sverilog", "+v2k"}:
                pass
            elif tok.startswith(("+", "-")):
                raise SystemExit(
                    f"Unsupported filelist option {tok!r} in {path}; "
                    "supported: -f/-F, -v, -y, +libext+, -I/+incdir+, -D/+define+"
                )
            else:
                self.files.append(_resolve(tok, base))
            i += 1

    def _add_defines(self, raw: str) -> None:
        self.defines.extend(item for item in raw.split("+") if item)


def parse_filelist(path: Path) -> Filelist:
    return Parser().parse(path)
