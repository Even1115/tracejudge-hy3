"""Offline-only rescoring of explicitly bound predictions against revised labels."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv=None):
    if sys.version_info < (3, 11):  # noqa: UP036 -- direct invocation outside package metadata
        print(
            "[blocked] Python 3.11+ required; use the configured WSL environment.", file=sys.stderr
        )
        return 2
    from tracejudge_hy3.process_eval_v2.label_revision import rescore_revision
    from tracejudge_hy3.process_eval_v2.materials import canonical
    from tracejudge_hy3.process_eval_v2.preflight import contained

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    target = contained(root, args.output)
    allowed = root / "artifacts/experiments/process-method-v2"
    if target == allowed or not target.is_relative_to(allowed):
        raise ValueError("output must be a new process-method-v2 subdirectory")
    report = rescore_revision(root)
    target.mkdir(parents=True, exist_ok=False)
    (target / "label-rescore.json").write_bytes(canonical(report) + b"\n")
    print(f"[offline] {target / 'label-rescore.json'}; model calls=0; executions=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
