"""Private oracle compression must preserve all existing sandbox limits."""

import os
import pickle
import subprocess
import sys
from pathlib import Path

import pytest

from tracejudge_hy3.evalplus import container_entrypoint as entry
from tracejudge_hy3.evalplus.docker_runner import EvalPlusDockerRunner


def test_private_pickle_round_trip_and_result_delegation(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    path = cache / ("a" * 32 + ".pkl")
    expected = {"base": ["abc" * 100000], "plus": [1, 2, 3], "time": [0.123]}
    with entry._cache_aware_parent_open(cache, open, path, "wb") as stream:
        pickle.dump(expected, stream)
    assert path.read_bytes()[:2] == b"\x1f\x8b"
    with entry._cache_aware_parent_open(cache, open, path, "rb") as stream:
        assert pickle.load(stream) == expected
    calls = []
    sentinel = object()

    def parent(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    for value in (tmp_path / "sample_eval_results.json", cache / "not-an-oracle.pkl"):
        assert entry._cache_aware_parent_open(cache, parent, value, "w") is sentinel
    assert len(calls) == 2


def test_cache_does_not_follow_links_or_overwrite(tmp_path):
    target = tmp_path / "original"
    target.write_bytes(b"preserve")
    link = tmp_path / ("b" * 32 + ".pkl")
    link.symlink_to(target)
    for mode in ("wb", "rb"):
        with pytest.raises(OSError), entry._gzip_reference_cache(link, mode):
            pass
    assert target.read_bytes() == b"preserve"
    with pytest.raises(OSError), entry._gzip_reference_cache(target, "wb"):
        pass


def test_cache_compression_under_real_file_limit(tmp_path):
    # Lower only this child process' limit; demonstrate a logical stream larger
    # than the limit without relaxing it or buffering the entire stream.
    source = """
import resource, sys
from pathlib import Path
from tracejudge_hy3.evalplus.container_entrypoint import _gzip_reference_cache
resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
path = Path(sys.argv[1])
with _gzip_reference_cache(path, 'wb') as f:
    for _ in range(128):
        f.write(b'a' * 8192)
assert path.stat().st_size < 65536
with _gzip_reference_cache(path, 'rb') as f:
    for _ in range(128):
        assert f.read(8192) == b'a' * 8192
    assert f.read(1) == b''
assert resource.getrlimit(resource.RLIMIT_FSIZE) == (65536, 65536)
"""
    result = subprocess.run(
        [sys.executable, "-c", source, str(tmp_path / "cache.pkl")],
        capture_output=True,
        timeout=15,
        env={**os.environ, "PYTHONPATH": str(Path(entry.__file__).resolve().parents[2])},
    )
    assert result.returncode == 0


def test_cache_option_is_opt_in_and_does_not_change_limits(tmp_path):
    original = EvalPlusDockerRunner()
    compressed = EvalPlusDockerRunner(reference_cache_compression="gzip")
    before = dict(original.public_identity())
    after = dict(compressed.public_identity())
    assert "reference_cache" not in before
    assert after.pop("reference_cache")["storage"] == "gzip-stream-v1"
    assert before == after
    files = (tmp_path / "raw", tmp_path / "control")
    command = compressed._container_command(
        tmp_path, "owned", ["run", "request"], detached=True, output_files=files
    )
    assert command[command.index("--ulimit") + 1] == "fsize=134217728:134217728"
    assert command[-2:] == ["--reference-cache-compression", "gzip"]
    with pytest.raises(ValueError):
        EvalPlusDockerRunner(reference_cache_compression="unbounded")
