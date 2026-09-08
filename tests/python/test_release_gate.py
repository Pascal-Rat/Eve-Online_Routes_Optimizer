from __future__ import annotations

import io
import tarfile
from pathlib import Path
from zipfile import ZipFile

import pytest

from tools.check_release import validate_distributions


def distributions(
    root: Path, *, wheel_version: str = "1.5.0", source_version: str = "1.5.0"
) -> Path:
    (root / "pyproject.toml").write_text('[project]\nversion = "1.5.0"\n')
    directory = root / "dist"
    directory.mkdir()
    with ZipFile(directory / "test.whl", "w") as archive:
        archive.writestr("test.dist-info/METADATA", f"Version: {wheel_version}\n")
    with tarfile.open(directory / "test.tar.gz", "w:gz") as archive:
        content = f"Version: {source_version}\n".encode()
        member = tarfile.TarInfo("test/PKG-INFO")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return directory


def test_release_manifest_identifies_exact_validated_artifacts(tmp_path: Path) -> None:
    directory = distributions(tmp_path)
    manifest = validate_distributions(tmp_path, directory, "v1.5.0")
    assert manifest["version"] == "1.5.0"
    first_hashes = manifest["sha256"]
    with ZipFile(directory / "test.whl", "a") as archive:
        archive.writestr("extra-file", "changed after validation")
    assert validate_distributions(tmp_path, directory)["sha256"] != first_hashes


def test_mismatched_tag_fails_before_artifact_promotion(tmp_path: Path) -> None:
    directory = distributions(tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        validate_distributions(tmp_path, directory, "v2.0.0")


@pytest.mark.parametrize("artifact", ["wheel", "source"])
def test_mismatched_distribution_version_is_rejected(tmp_path: Path, artifact: str) -> None:
    directory = distributions(
        tmp_path,
        wheel_version="0.0.0" if artifact == "wheel" else "1.5.0",
        source_version="0.0.0" if artifact == "source" else "1.5.0",
    )
    with pytest.raises(ValueError, match="distribution versions"):
        validate_distributions(tmp_path, directory, "v1.5.0")
