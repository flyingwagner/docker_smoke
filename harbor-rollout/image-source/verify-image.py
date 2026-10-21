"""Offline image acceptance: exact Codex bytes, Python version and audited pip packages."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    inventory = json.loads(Path(__file__).with_name("h200-image-inventory.json").read_text())
    assert sys.version_info[:3] == (3, 11, 2), sys.version
    assert subprocess.check_output(["codex", "--version"], text=True).strip() == inventory["codex_version"]
    for name, info in inventory["binaries"].items():
        actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
        assert actual == info["sha256"], f"binary mismatch: {name}: {actual}"
    excluded = {"pip", "setuptools", "wheel"}  # Debian-managed bootstrap packages.
    for name, version in inventory["packages"].items():
        if name not in excluded:
            actual = importlib.metadata.version(name)
            assert actual == version, f"package mismatch: {name}: {actual} != {version}"
    process_tools = {name: shutil.which(name) for name in ("ps", "pkill", "pgrep", "free")}
    assert all(process_tools.values()), f"missing process tools: {process_tools}"
    import numpy, scipy, sympy, mpmath, requests, openai, yaml
    print(json.dumps({"python": sys.version.split()[0], "codex": inventory["codex_version"],
                      "verified_packages": len(inventory["packages"]) - len(excluded),
                      "process_tools": process_tools,
                      "binary_hashes_match_h200": True}, sort_keys=True))


if __name__ == "__main__":
    main()
