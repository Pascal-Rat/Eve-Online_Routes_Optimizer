from __future__ import annotations

from pathlib import Path

import pytest

from eve_courier_optimizer.jsonio import read_json_object, write_json


def test_failed_publication_preserves_previous_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "execution.json"
    write_json(path, {"accepted": [1, 2]})

    def unavailable(_descriptor: int) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr("eve_courier_optimizer.jsonio.os.fsync", unavailable)
    with pytest.raises(OSError, match="disk unavailable"):
        write_json(path, {"accepted": [3]})
    assert read_json_object(path) == {"accepted": [1, 2]}
    assert tuple(tmp_path.iterdir()) == (path,)


def test_nonfinite_values_cannot_replace_an_artifact(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    write_json(path, {"reward": 100})
    with pytest.raises(ValueError):
        write_json(path, {"reward": float("nan")})
    assert read_json_object(path) == {"reward": 100}


@pytest.mark.parametrize("version", [True, 1.0, "1", None, []])
def test_artifact_schema_requires_an_integer(version: object) -> None:
    from eve_courier_optimizer.application.execution import execution_state_from_dict
    from eve_courier_optimizer.eve.snapshot import snapshot_from_dict

    for decode in (execution_state_from_dict, snapshot_from_dict):
        with pytest.raises(ValueError, match="schema_version must be an integer"):
            decode({"schema_version": version})
