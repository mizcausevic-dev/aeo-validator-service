"""Reject unexpected files in the release wheel and source archive."""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path
from sys import argv


def main() -> None:
    dist = Path(argv[1]) if len(argv) > 1 else Path("dist")
    sdists = list(dist.glob("*.tar.gz"))
    wheels = list(dist.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise SystemExit("expected exactly one sdist and one wheel in dist/")

    with tarfile.open(sdists[0]) as archive:
        names = [member.name.partition("/")[2] for member in archive.getmembers() if member.isfile()]
    allowed_root = {".gitignore", "HOSTED_PILOT.md", "LICENSE", "README.md", "pyproject.toml", "PKG-INFO"}
    unexpected_sdist = [
        name
        for name in names
        if name not in allowed_root
        and not (
            name.startswith("src/aeo_validator_service/")
            and (name.endswith(".py") or name.endswith("/py.typed"))
        )
        and not (name.startswith("tests/") and name.endswith(".py"))
    ]
    if not names or unexpected_sdist:
        raise SystemExit(f"unexpected sdist entries: {unexpected_sdist}")
    if "src/aeo_validator_service/py.typed" not in names:
        raise SystemExit("sdist is missing py.typed")

    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_names = archive.namelist()
    unexpected_wheel = [
        name
        for name in wheel_names
        if not name.startswith("aeo_validator_service/") and not name.startswith("aeo_validator_service-")
    ]
    if unexpected_wheel:
        raise SystemExit(f"unexpected wheel entries: {unexpected_wheel}")
    if "aeo_validator_service/py.typed" not in wheel_names:
        raise SystemExit("wheel is missing py.typed")
    print(f"distribution contents passed: {len(names)} sdist files, {len(wheel_names)} wheel files")


if __name__ == "__main__":
    main()
