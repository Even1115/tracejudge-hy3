"""Read-only public-package checks. No private artifacts, network or third-party deps."""

import argparse
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = Path(__file__).resolve().parent
PREFIX = "docs/evaluation_release/2026-09-06/"
REPORT = "docs/two_dataset_interim_evaluation_report.md"
PAYLOAD_FILES = {
    REPORT,
    *(
        PREFIX + name
        for name in (
            "README.md",
            "results.json",
            "configurations.json",
            "source_hashes.json",
            "livecodebench_handoff.md",
            "verify_release.py",
        )
    ),
}
PROHIBITED_KEYS = {
    "api_key",
    "authorization",
    "endpoint",
    "endpoint_sha256",
    "base_url",
    "raw_output",
    "raw_output_attempt",
    "raw_response",
    "candidate_code",
    "solution",
    "solution_trace",
    "test_cases",
    "hidden_tests",
    "messages",
    "prompt",
}
PRIVATE_STRING = re.compile(
    r"/Users/|/home/|Bearer\s+\S+|\bsk-[A-Za-z0-9_-]{12,}|\bghp_[A-Za-z0-9]{12,}"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(name):
    return json.loads((PACKAGE / name).read_text(encoding="utf-8"))


def check_tree(value, field="root"):
    if isinstance(value, dict):
        require(not PROHIBITED_KEYS.intersection(value), f"Prohibited field under {field}")
        if "numerator" in value and "denominator" in value:
            n, d = value["numerator"], value["denominator"]
            require(0 <= n <= d, f"Invalid counts: {field}")
            observed = value.get("value", value.get("estimate"))
            if d and observed is not None:
                require(math.isclose(n / d, observed, abs_tol=1e-12), f"Fraction mismatch: {field}")
        for key, item in value.items():
            check_tree(item, field + "." + key)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_tree(item, f"{field}[{index}]")
    elif isinstance(value, str):
        require(not PRIVATE_STRING.search(value), f"Potential private string at {field}")


def wilson(k, n):
    z = 1.959963984540054
    p = k / n
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return center - half, center + half


def markdown_anchors(content):
    result, counts = set(), {}
    for heading in re.findall(r"^#{1,6}\s+(.+)$", content, re.M):
        base = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        result.add(base if not occurrence else f"{base}-{occurrence}")
    result.update(re.findall(r'<a\s+(?:id|name)="([^"]+)"', content))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-index",
        action="store_true",
        help="Also check publication files and local links against Git's index.",
    )
    args = parser.parse_args()
    checksum_path = PACKAGE / "SHA256SUMS"
    checksum_rows = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  (.+)", line)
        require(match is not None, "Malformed SHA256SUMS line")
        digest, name = match.groups()
        require(
            name in PAYLOAD_FILES and name not in checksum_rows,
            "Unexpected or duplicate checksum path",
        )
        checksum_rows[name] = digest
        path = ROOT / name
        require(path.is_file() and not path.is_symlink(), f"Not a regular public file: {name}")
        require(sha(path) == digest, f"Hash mismatch: {name}")
    require(set(checksum_rows) == PAYLOAD_FILES, "Incomplete checksum coverage")

    index_targets = PAYLOAD_FILES | {PREFIX + "SHA256SUMS"}
    link_count = 0
    for name in sorted(PAYLOAD_FILES):
        path = ROOT / name
        if path.suffix != ".md":
            continue
        content = path.read_text(encoding="utf-8")
        require(not PRIVATE_STRING.search(content), f"Potential private text: {name}")
        for target in re.findall(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", content):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc:
                require(
                    parsed.scheme == "https" and parsed.hostname == "livecodebench.github.io",
                    "Unexpected external link; review before publication",
                )
                continue
            resolved = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
            require(resolved.is_relative_to(ROOT), "Local link escapes repository")
            relative = resolved.relative_to(ROOT).as_posix()
            require(resolved.is_file(), f"Broken local link: {relative}")
            require(
                not relative.startswith(("artifacts/", "data/")),
                "Link targets private experiment storage",
            )
            if parsed.fragment and resolved.suffix == ".md":
                require(
                    unquote(parsed.fragment)
                    in markdown_anchors(resolved.read_text(encoding="utf-8")),
                    f"Broken anchor: {relative}",
                )
            index_targets.add(relative)
            link_count += 1

    results, configs, sources = [
        read_json(name) for name in ("results.json", "configurations.json", "source_hashes.json")
    ]
    for payload in (results, configs, sources):
        check_tree(payload)
    source_rows = sources["sources"]
    ids = {row["source_id"] for row in source_rows}
    require(len(ids) == len(source_rows) == 36, "Source count or identity mismatch")
    require(
        all(
            re.fullmatch(r"[a-f0-9]{64}", row["sha256"])
            and row["size_bytes"] > 0
            and row["raw_content_in_public_package"] is False
            for row in source_rows
        ),
        "Invalid source metadata",
    )

    def check_source_ids(value):
        if isinstance(value, dict):
            if "source_ids" in value:
                require(set(value["source_ids"]).issubset(ids), "Unknown source reference")
            for item in value.values():
                check_source_ids(item)
        elif isinstance(value, list):
            for item in value:
                check_source_ids(item)

    check_source_ids(results)
    he = results["humanevalplus"]["execution"]
    require(
        (
            he["actual_execution_count"],
            he["base_pass_count"],
            he["base_plus_pass_count"],
            he["infrastructure_error_count"],
        )
        == (164, 163, 157, 0),
        "HumanEval summary mismatch",
    )
    mb = results["mbppplus"]
    require(
        mb["metrics"]["planned_n"] == mb["metrics"]["actual_execution_n"] == 120,
        "MBPP coverage mismatch",
    )
    for metric, k in (("base_pass_full_denominator", 117), ("base_plus_pass_full_denominator", 95)):
        fraction = mb["metrics"][metric]
        require((fraction["numerator"], fraction["denominator"]) == (k, 120), "MBPP count mismatch")
        require(
            all(
                math.isclose(a, b, abs_tol=1e-12)
                for a, b in zip(wilson(k, 120), fraction["wilson_95"], strict=True)
            ),
            "MBPP interval mismatch",
        )
    require(
        mb["base_extra_status_counts"]
        == {"pass_pass": 95, "pass_fail": 20, "pass_timeout": 2, "fail_fail": 3},
        "MBPP status mismatch",
    )
    a = results["codejudge_eval"]["experiments"]["v3a_complete"]
    require(
        a["binary_metrics"]["valid_confusion"] == {"tp": 59, "tn": 49, "fp": 11, "fn": 1},
        "CodeJudge A mismatch",
    )
    require(
        math.isclose(a["primary"]["balanced_accuracy_full_denominator"], 0.9),
        "CodeJudge balanced accuracy mismatch",
    )
    b = results["codejudge_eval"]["experiments"]["v3b"]
    require(b["analysis_population"]["complete_triplet_n"] == 50, "CodeJudge B coverage mismatch")
    for condition, native in (("easy", 48), ("middle", 42), ("hard", 36)):
        row = b["per_condition"][condition]
        require(
            row["functional_accuracy_full_denominator"]["numerator"] == 49
            and row["native_label_exact_match_full_denominator"]["numerator"] == native,
            "CodeJudge B score mismatch",
        )
    lcb = results["livecodebench"]
    require(
        lcb["planned_task_n"] == 60
        and lcb["results"] is None
        and lcb["run_id"] is None
        and lcb["status"] == "RUNNING_ON_OTHER_COMPUTER_RESULTS_UNVERIFIED",
        "Unverified LCB result present",
    )
    if args.check_index:
        for name in sorted(index_targets):
            staged = subprocess.run(
                ["git", "show", ":" + name], cwd=ROOT, capture_output=True, check=False
            )
            require(staged.returncode == 0, f"File absent from Git index: {name}")
            require(
                staged.stdout == (ROOT / name).read_bytes(),
                f"Index differs from working file: {name}",
            )
    print(
        json.dumps(
            {
                "status": "PASS",
                "public_files_hashed": len(checksum_rows),
                "local_links_checked": link_count,
                "source_hash_references": len(ids),
                "index_checked": args.check_index,
                "scope": "Public-package consistency only; no private-data rescoring or remote experiment verification.",
            }
        )
    )


if __name__ == "__main__":
    main()
