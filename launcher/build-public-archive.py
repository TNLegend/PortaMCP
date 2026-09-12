from __future__ import annotations

import argparse
import hashlib
import stat
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXECUTABLE_PATHS = {
    "PortaMCP",
    "portamcp.sh",
    "launcher/build-launcher-linux.sh",
}
ROOT_FILES = {
    ".gitattributes",
    ".gitignore",
    "LICENSE",
    "MANIFEST.in",
    "PortaMCP",
    "PortaMCP.exe",
    "README.md",
    "portamcp.pyw",
    "portamcp.sh",
    "pyproject.toml",
    "requirements.txt",
}
PUBLIC_TREES = {
    "assets",
    "launcher",
    "porta_mcp",
    "tests",
}
RUNTIME_FILES = {
    "runtime/python-bootstrap.zip",
    "runtime/python-bootstrap.sha256",
}
CHROME_FILES = {
    "chrome_extension/manifest.json",
    "chrome_extension/service_worker.js",
}
EXCLUDED_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".venv-linux",
    ".portamcp",
    "build",
    "dist",
    "portamcp.egg-info",
    "config.json",
    "bridge_config.js",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log", ".jsonl", ".pem", ".key", ".secret"}


def _public_files() -> list[Path]:
    files: set[Path] = set()
    for name in ROOT_FILES:
        path = ROOT / name
        if not path.is_file():
            raise FileNotFoundError(f"Required public file is missing: {name}")
        files.add(path)
    for rel in RUNTIME_FILES | CHROME_FILES:
        path = ROOT / rel
        if not path.is_file():
            raise FileNotFoundError(f"Required public file is missing: {rel}")
        files.add(path)
    for tree in PUBLIC_TREES:
        base = ROOT / tree
        if not base.is_dir():
            raise FileNotFoundError(f"Required public directory is missing: {tree}")
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if any(part in EXCLUDED_NAMES for part in rel.parts):
                continue
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            if path.name.startswith("AUDIT_"):
                continue
            files.add(path)
    return sorted(files, key=lambda path: path.relative_to(ROOT).as_posix())


def build_archive(output: Path) -> tuple[int, str]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    files = _public_files()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            rel = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo.from_file(path, arcname=rel)
            info.create_system = 3  # Unix metadata, even when built on Windows.
            mode = stat.S_IFREG | (0o755 if rel in EXECUTABLE_PATHS else 0o644)
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, archive.open(info, "w") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return len(files), digest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a sanitized PortaMCP public archive from the repository root.")
    parser.add_argument("output", nargs="?", default=str(ROOT / "dist" / "PortaMCP-public.zip"))
    args = parser.parse_args()
    output = Path(args.output)
    count, digest = build_archive(output)
    print(f"Built {output.resolve()} with {count} files")
    print(f"SHA256 {digest}")


if __name__ == "__main__":
    main()
