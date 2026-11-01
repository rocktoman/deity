from pathlib import Path

import pytest

from deity.adapters.execution.artifacts import ArtifactApplier
from deity.adapters.execution.workspace import collect_manifest, file_hashes


def test_apply_reviewed_text_without_overwriting_drift(tmp_path: Path) -> None:
    host = tmp_path / "host"
    stage = tmp_path / "stage"
    host.mkdir()
    stage.mkdir()
    (host / "a.txt").write_text("before\n", encoding="utf-8")
    (stage / "a.txt").write_text("before\n", encoding="utf-8")
    baseline = file_hashes(stage)
    (stage / "a.txt").write_text("after\n", encoding="utf-8")
    manifest = collect_manifest("execution", baseline, file_hashes(stage))
    applier = ArtifactApplier(host, stage, baseline)
    change = applier.apply(manifest.entries[0], "call")
    assert change.after_sha256 == file_hashes(host)["a.txt"][0]
    assert (host / "a.txt").read_text() == "after\n"

    (host / "a.txt").write_text("user edit", encoding="utf-8")
    with pytest.raises(ValueError, match="preimage"):
        applier.apply(manifest.entries[0], "call")


def test_artifact_symlink_and_binary_denied(tmp_path: Path) -> None:
    host = tmp_path / "host"
    stage = tmp_path / "stage"
    host.mkdir()
    stage.mkdir()
    (stage / "binary.dat").write_bytes(b"\0\1")
    entry = collect_manifest("execution", {}, file_hashes(stage)).entries[0]
    applier = ArtifactApplier(host, stage, {})
    with pytest.raises(ValueError, match="binary"):
        applier.apply(entry, "call")
    (host / "binary.dat").symlink_to(stage / "binary.dat")
    with pytest.raises(ValueError, match="symlink"):
        applier.apply(entry, "call")


def test_artifact_create_and_delete(tmp_path: Path) -> None:
    host = tmp_path / "host"
    stage = tmp_path / "stage"
    host.mkdir()
    stage.mkdir()
    (host / "old.txt").write_text("old", encoding="utf-8")
    (stage / "old.txt").write_text("old", encoding="utf-8")
    baseline = file_hashes(stage)
    (stage / "old.txt").unlink()
    (stage / "new.txt").write_text("new", encoding="utf-8")
    entries = collect_manifest("execution", baseline, file_hashes(stage)).entries
    applier = ArtifactApplier(host, stage, baseline)
    changes = [applier.apply(entry, "call") for entry in entries]
    assert {change.operation for change in changes} == {"create", "delete"}
    assert not (host / "old.txt").exists()
    assert (host / "new.txt").read_text() == "new"
