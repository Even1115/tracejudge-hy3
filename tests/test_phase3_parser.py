from __future__ import annotations

import pytest

from tracejudge_hy3.phase3.parser import (
    StrictJudgmentParseError,
    parse_method_judgment,
)


def test_phase3_parser_accepts_only_one_complete_json_object():
    parsed = parse_method_judgment('{"functional_correct":true,"has_error":false}')
    assert parsed.functional_correct is True

    invalid = (
        '```json\n{"functional_correct":true,"has_error":false}\n```',
        'prefix {"functional_correct":true,"has_error":false}',
        '{"functional_correct":true,"has_error":false} suffix',
        '[{"functional_correct":true,"has_error":false}]',
    )
    for raw in invalid:
        with pytest.raises(StrictJudgmentParseError):
            parse_method_judgment(raw)


def test_phase3_parser_schema_diagnostic_does_not_echo_input_values():
    canary = "PROVIDER-RAW-CANARY-MUST-NOT-ECHO"
    with pytest.raises(StrictJudgmentParseError) as exc_info:
        parse_method_judgment(
            '{"functional_correct":true,"has_error":false,"unexpected":"' + canary + '"}'
        )

    assert exc_info.value.diagnostic_code == "schema_validation_failed"
    assert canary not in exc_info.value.safe_diagnostic


def test_phase3_parser_enforces_error_evidence_semantics():
    with pytest.raises(StrictJudgmentParseError) as exc_info:
        parse_method_judgment('{"functional_correct":false,"has_error":true}')

    assert exc_info.value.diagnostic_code == "schema_validation_failed"
