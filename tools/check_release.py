"""Verify tag/source/distribution versions and record the exact artifacts promoted by CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tomllib
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile


def validate_distributions(root: Path, directory: Path, tag: str = "") -> dict[str, object]:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if tag and tag != f"v{version}":
        raise ValueError(f"release tag {tag!r} does not match project version v{version}")
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("release requires exactly one wheel and one source distribution")
    wheel, source = wheels[0], sources[0]
    with ZipFile(wheel) as archive:
        metadata = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("wheel must contain exactly one distribution metadata record")
        wheel_version = BytesParser().parsebytes(archive.read(metadata[0]))["Version"]
    with tarfile.open(source) as archive:
        source_metadata = [
            member
            for member in archive.getmembers()
            if member.name.count("/") == 1 and member.name.endswith("/PKG-INFO")
        ]
        if len(source_metadata) != 1:
            raise ValueError("source distribution must contain root PKG-INFO")
        stream = archive.extractfile(source_metadata[0])
        if stream is None:
            raise ValueError("source distribution metadata is not a file")
        with stream:
            source_version = BytesParser().parsebytes(stream.read())["Version"]
    if wheel_version != version or source_version != version:
        raise ValueError("built distribution versions do not match pyproject.toml")
    return {
        "version": version,
        "sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (wheel, source)
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="")
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--verify-manifest", action="store_true")
    args = parser.parse_args()
    manifest = validate_distributions(Path(__file__).resolve().parents[1], args.dist, args.tag)
    path = args.dist / "release-manifest.json"
    if args.verify_manifest:
        if json.loads(path.read_text()) != manifest:
            raise ValueError("release artifacts differ from the validated build")
    else:
        path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Validated distributions for {manifest['version']}")


if __name__ == "__main__":
    main()
