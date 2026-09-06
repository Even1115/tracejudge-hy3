"""The supplementary probe changes only exception reporting, not evaluation."""

import ast
import importlib
from pathlib import Path

import pytest


def module(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    return importlib.import_module("diagnose_humanevalplus_internal_error")


def test_instrumentation_keeps_guarded_evaluation_identical(monkeypatch):
    diagnostic = module(monkeypatch)
    source = (
        diagnostic.base.ROOT / "src/tracejudge_hy3/evalplus/container_entrypoint.py"
    ).read_text()
    transformed = diagnostic.instrument(source)
    trees = [ast.parse(s) for s in (source, transformed)]
    guarded = [
        next(
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "_guarded_official_evaluate"
        )
        for tree in trees
    ]
    assert ast.dump(guarded[0]) == ast.dump(guarded[1])
    assert len(diagnostic.ERROR_CODES) == len(set(diagnostic.ERROR_CODES.values()))
    assert min(diagnostic.ERROR_CODES) >= 100
    assert max(diagnostic.ERROR_CODES) < 255
    compile(transformed, "diagnostic_entrypoint.py", "exec")


def test_unknown_wrapper_fails_closed(monkeypatch):
    diagnostic = module(monkeypatch)
    with pytest.raises(ValueError, match="no longer matches"):
        diagnostic.instrument("unknown wrapper")


def test_probe_identity_explicitly_not_formal_scoring(monkeypatch):
    diagnostic = module(monkeypatch)
    evidence = diagnostic.InternalErrorRunner().public_identity()["diagnostic_instrumentation"]
    assert evidence["formal_scoring_eligible"] is False
    assert evidence["original_wrapper_sha256"] != evidence["staged_wrapper_sha256"]


@pytest.mark.parametrize("number,groundtruth", [(27, False), (28, False), (27, True)])
def test_exception_encoding_is_typed_and_does_not_expose_message(
    monkeypatch, capsys, number, groundtruth
):
    diagnostic = module(monkeypatch)
    runner = diagnostic.InternalErrorRunner()
    namespace = {"__name__": "diagnostic_entrypoint_test"}
    exec(compile(runner.instrumented, "diagnostic_entrypoint.py", "exec"), namespace)
    fake_source = f'def get_groundtruth():\n    raise OSError({number}, "PRIVATE_PAYLOAD")\n'
    origin = "/trusted/evalplus/evaluate.py" if groundtruth else "unrelated.py"
    simulated = {}
    exec(compile(fake_source, origin, "exec"), simulated)

    def guarded(*_):
        try:
            simulated["get_groundtruth"]()
        except OSError:
            raise namespace["_EntrypointError"]("executor_failed") from None

    namespace["_guarded_official_evaluate"] = guarded
    result = namespace["main"]([namespace["_INTERNAL_EVALUATE_MODE"], *(["unused"] * 4)])
    assert result == (160 if groundtruth else diagnostic.OS_EXIT_CODES[number])
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
