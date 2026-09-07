"""Check built archives for unintended local inputs/state/configuration; never print file contents."""
from __future__ import annotations

import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath


def check(names):
    forbidden = {".repo", ".mcp-state", ".inputdata", ".git", ".venv", "erpnext-dev", "__pycache__"}
    for name in names:
        path = PurePosixPath(name)
        if forbidden.intersection(path.parts):
            raise SystemExit(f"Private/local directory in release: {name}")
        if path.name == "AGENTS.md" or path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            raise SystemExit(f"Private configuration in release: {name}")
        if "allowlist" in path.name or path.suffix in {".sqlite3", ".pyc"}:
            raise SystemExit(f"Retired/runtime artifact in release: {name}")


version = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
archives = list(Path("dist").glob(f"erpnext_mcp-{version}*"))
if not any(p.suffix == ".whl" for p in archives) or not any(p.name.endswith(".tar.gz") for p in archives):
    raise SystemExit("Build both wheel and sdist first")
for archive in archives:
    if archive.suffix == ".whl":
        with zipfile.ZipFile(archive) as package:
            names = package.namelist()
    else:
        with tarfile.open(archive) as package:
            names = package.getnames()
    check(names)
    print(f"{archive.name}: {len(names)} entries, no local/private/retired artifacts")
