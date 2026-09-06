#!/usr/bin/env python3
"""Diagnostic-only exception classification; never alters official evaluation.

The staged wrapper encodes only allowlisted exception categories into the child
exit code. No exception messages, tracebacks, samples, or tests are disclosed.
Production source and formal artifacts remain unchanged.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import diagnose_humanevalplus_pending as base

REASONS = (
    "result_integrity_failed",
    "runtime_identity_mismatch",
    "executor_failed",
    "other",
)
CAUSES = (
    "none",
    "AssertionError",
    "ValueError",
    "TypeError",
    "RuntimeError",
    "OSError",
    "MemoryError",
    "KeyError",
    "other",
)
ERROR_CODES = {
    100 + i * len(CAUSES) + j: f"diagnostic_{reason}_{cause.lower()}"
    for i, reason in enumerate(REASONS)
    for j, cause in enumerate(CAUSES)
}
OS_ERRORS = {
    5: "io_error",
    11: "resource_temporarily_unavailable",
    12: "cannot_allocate_memory",
    13: "permission_denied",
    24: "too_many_open_files",
    27: "file_too_large",
    28: "no_space_left_on_device",
    30: "read_only_filesystem",
    38: "function_not_implemented",
}
OS_EXIT_CODES = {number: 150 + i for i, number in enumerate(OS_ERRORS)}
ERROR_CODES.update(
    {
        OS_EXIT_CODES[number]: f"diagnostic_oserror_{number}_{name}"
        for number, name in OS_ERRORS.items()
    }
)
ERROR_CODES[160] = "diagnostic_oserror27_groundtruth_cache"


def instrument(source: str) -> str:
    old_child = "        except BaseException:\n            return 73\n"
    new_child = f"""        except BaseException as diagnostic_exception:
            diagnostic_reason = (
                diagnostic_exception.code
                if isinstance(diagnostic_exception, _EntrypointError)
                else "other"
            )
            diagnostic_reasons = {REASONS!r}
            if diagnostic_reason not in diagnostic_reasons:
                diagnostic_reason = "other"
            diagnostic_context = diagnostic_exception.__context__
            if type(diagnostic_context) is OSError and diagnostic_context.errno == 27:
                diagnostic_tb = diagnostic_context.__traceback__
                while diagnostic_tb is not None:
                    diagnostic_frame = diagnostic_tb.tb_frame.f_code
                    if (diagnostic_frame.co_filename.endswith("/evalplus/evaluate.py")
                            and diagnostic_frame.co_name == "get_groundtruth"):
                        return 160
                    diagnostic_tb = diagnostic_tb.tb_next
            diagnostic_os_codes = {OS_EXIT_CODES!r}
            if (type(diagnostic_context) is OSError
                    and diagnostic_context.errno in diagnostic_os_codes):
                return diagnostic_os_codes[diagnostic_context.errno]
            diagnostic_cause = (
                "none" if diagnostic_context is None
                else type(diagnostic_context).__name__
            )
            diagnostic_causes = {CAUSES!r}
            if diagnostic_cause not in diagnostic_causes:
                diagnostic_cause = "other"
            return (100 + diagnostic_reasons.index(diagnostic_reason) * {len(CAUSES)}
                    + diagnostic_causes.index(diagnostic_cause))
"""
    old_parent = "            if completed.returncode == 73:\n"
    new_parent = f"""            diagnostic_codes = {ERROR_CODES!r}
            if completed.returncode in diagnostic_codes:
                raise _EntrypointError(diagnostic_codes[completed.returncode])
            if completed.returncode == 73:
"""
    if source.count(old_child) != 1 or source.count(old_parent) != 1:
        raise ValueError("diagnostic transformation no longer matches frozen wrapper")
    return source.replace(old_child, new_child).replace(old_parent, new_parent)


class InternalErrorRunner(base.DiagnosticRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.original = (
            base.ROOT / "src/tracejudge_hy3/evalplus/container_entrypoint.py"
        ).read_bytes()
        self.instrumented = instrument(self.original.decode()).encode()
        self.control_errors.update(ERROR_CODES.values())

    def public_identity(self):
        identity = super().public_identity()
        identity["diagnostic_instrumentation"] = {
            "formal_scoring_eligible": False,
            "changes": "child_exception_exit_code_classification_only",
            "original_wrapper_sha256": hashlib.sha256(self.original).hexdigest(),
            "staged_wrapper_sha256": hashlib.sha256(self.instrumented).hexdigest(),
            "instrumentation_script_sha256": base.sha(Path(__file__)),
            "error_code_map": ERROR_CODES,
        }
        return identity

    def _stage_control_files(self, staging_value, **kwargs):
        super()._stage_control_files(staging_value, **kwargs)
        path = Path(staging_value) / "entrypoint.py"
        if path.read_bytes() != self.original:
            raise ValueError("staged source differs")
        # This file is an owned temporary diagnostic copy, not the source module.
        os.chmod(path, 0o600)
        try:
            path.write_bytes(self.instrumented)
        finally:
            os.chmod(path, 0o444)


if __name__ == "__main__":
    base.DiagnosticRunner = InternalErrorRunner
    raise SystemExit(base.main())
