"""V2 verification-chain and evaluation-report authority tests."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    EvalCaseResult,
    EvalMetricId,
    EvalRunSummary,
    GateId,
    GateReasonCode,
    VerificationAttempt,
    VerificationAttemptSeries,
)
from claimdone_api.gates.registry import make_gate_decision

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
HAPPY_PATH = REPOSITORY_ROOT / "contracts" / "examples" / "happy_path.json"


def happy_data() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(HAPPY_PATH.read_text(encoding="utf-8")))


def verified_report(*, verified_at: str = "2026-07-14T12:00:10Z") -> dict[str, Any]:
    report = deepcopy(happy_data()["verification"])
    report["verifiedAt"] = verified_at
    return cast(dict[str, Any], report)


def mismatch_report(
    *,
    verified_at: str = "2026-07-14T12:00:10Z",
    attachment_count: int = 3,
) -> dict[str, Any]:
    report = verified_report(verified_at=verified_at)
    location = next(result for result in report["fieldResults"] if result["field"] == "location")
    location["actual"] = "Berln"
    location["status"] = "mismatch"
    report.update(
        {
            "status": "mismatch",
            "deterministicMatch": False,
            "actualAttachmentCount": attachment_count,
            "reviewAllowed": False,
        }
    )
    return report


def passing_attempt_data() -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "attemptId": "verification-1",
        "caseId": "case-happy-001",
        "attemptNumber": 1,
        "caseState": "verifying",
        "portalVersion": 1,
        "report": verified_report(),
        "final": True,
        "repair": None,
        "repairedFromAttemptId": None,
        "gateDecision": make_gate_decision(
            GateId.G8_VERIFICATION,
            decided_at=datetime(2026, 7, 14, 12, 0, 11, tzinfo=UTC),
        ),
    }


def repairable_attempt_data() -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "attemptId": "verification-1",
        "caseId": "case-happy-001",
        "attemptNumber": 1,
        "caseState": "verifying",
        "portalVersion": 1,
        "report": mismatch_report(),
        "final": False,
        "repair": {
            "repairNumber": 1,
            "field": "location",
            "sourceRefs": ["prov-statement"],
            "fromPortalVersion": 1,
            "toPortalVersion": 2,
        },
        "repairedFromAttemptId": None,
        "gateDecision": None,
    }


def repaired_attempt_data() -> dict[str, Any]:
    data = passing_attempt_data()
    data.update(
        {
            "attemptId": "verification-2",
            "attemptNumber": 2,
            "portalVersion": 2,
            "report": verified_report(verified_at="2026-07-14T12:00:12Z"),
            "repairedFromAttemptId": "verification-1",
            "gateDecision": make_gate_decision(
                GateId.G8_VERIFICATION,
                decided_at=datetime(2026, 7, 14, 12, 0, 13, tzinfo=UTC),
            ),
        }
    )
    return data


def test_one_attempt_success_is_final_immutable_and_emits_only_derived_g8() -> None:
    attempt = VerificationAttempt.model_validate(passing_attempt_data())
    series = VerificationAttemptSeries.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": attempt.case_id,
            "attempts": [attempt],
        }
    )
    assert series.attempts == (attempt,)
    assert attempt.gate_decision is not None and attempt.gate_decision.passed
    with pytest.raises(ValidationError, match="frozen"):
        attempt.final = False


def test_single_field_repair_chain_binds_id_version_time_and_unchanged_fields() -> None:
    first = VerificationAttempt.model_validate(repairable_attempt_data())
    second = VerificationAttempt.model_validate(repaired_attempt_data())
    series = VerificationAttemptSeries.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "caseId": first.case_id,
            "attempts": [first, second],
        }
    )
    assert len(series.attempts) == 2

    for field, value in (
        ("repairedFromAttemptId", "other-attempt"),
        ("portalVersion", 3),
    ):
        unsafe_second = repaired_attempt_data()
        unsafe_second[field] = value
        with pytest.raises(ValidationError):
            VerificationAttemptSeries.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "caseId": first.case_id,
                    "attempts": [first, VerificationAttempt.model_validate(unsafe_second)],
                }
            )

    same_time = repaired_attempt_data()
    same_time["report"]["verifiedAt"] = "2026-07-14T12:00:10Z"
    same_time["gateDecision"] = make_gate_decision(
        GateId.G8_VERIFICATION,
        decided_at=datetime(2026, 7, 14, 12, 0, 11, tzinfo=UTC),
    )
    with pytest.raises(ValidationError, match="must not precede"):
        VerificationAttemptSeries.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": first.case_id,
                "attempts": [first, VerificationAttempt.model_validate(same_time)],
            }
        )

    changed_non_target = repaired_attempt_data()
    incident_time = next(
        result
        for result in changed_non_target["report"]["fieldResults"]
        if result["field"] == "incident_time"
    )
    incident_time["expected"] = "14:31:00"
    incident_time["actual"] = "14:31:00"
    with pytest.raises(ValidationError, match="expected values"):
        VerificationAttemptSeries.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": first.case_id,
                "attempts": [first, VerificationAttempt.model_validate(changed_non_target)],
            }
        )


def test_partial_or_third_repair_and_changed_attachments_are_impossible() -> None:
    partial = repairable_attempt_data()
    partial["report"]["fieldResults"] = [
        result for result in partial["report"]["fieldResults"] if result["field"] == "location"
    ]
    with pytest.raises(ValidationError, match="complete scalar"):
        VerificationAttempt.model_validate(partial)

    third = repaired_attempt_data()
    third["attemptNumber"] = 3
    with pytest.raises(ValidationError):
        VerificationAttempt.model_validate(third)

    first = VerificationAttempt.model_validate(repairable_attempt_data())
    changed_attachments = repaired_attempt_data()
    changed_attachments["report"] = mismatch_report(
        verified_at="2026-07-14T12:00:12Z",
        attachment_count=2,
    )
    changed_attachments["gateDecision"] = make_gate_decision(
        GateId.G8_VERIFICATION,
        deterministic_reasons=(
            GateReasonCode.G8_FIELD_MISMATCH,
            GateReasonCode.G8_ATTACHMENT_MISMATCH,
        ),
        decided_at=datetime(2026, 7, 14, 12, 0, 13, tzinfo=UTC),
    )
    with pytest.raises(ValidationError, match="attachment"):
        VerificationAttemptSeries.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "caseId": first.case_id,
                "attempts": [
                    first,
                    VerificationAttempt.model_validate(changed_attachments),
                ],
            }
        )


def test_final_g8_reasons_are_exact_and_registry_ordered() -> None:
    data = passing_attempt_data()
    data["report"] = mismatch_report(attachment_count=2)
    data["gateDecision"] = make_gate_decision(
        GateId.G8_VERIFICATION,
        deterministic_reasons=(
            GateReasonCode.G8_FIELD_MISMATCH,
            GateReasonCode.G8_ATTACHMENT_MISMATCH,
        ),
        decided_at=datetime(2026, 7, 14, 12, 0, 11, tzinfo=UTC),
    )
    assert VerificationAttempt.model_validate(data).final

    reversed_gate = data["gateDecision"].model_dump(mode="json", by_alias=True)
    reversed_gate["reasonCodes"] = [
        "G8_ATTACHMENT_MISMATCH",
        "G8_FIELD_MISMATCH",
    ]
    data["gateDecision"] = reversed_gate
    with pytest.raises(ValidationError, match="derived"):
        VerificationAttempt.model_validate(data)


def check_data(
    metric_id: str,
    *,
    passed: bool = True,
    observed_gate_id: str | None = None,
    observed_reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "metricId": metric_id,
        "graderType": "deterministic",
        "passed": passed,
        "score": None,
        "failureCode": None if passed else "expectation_mismatch",
        "observedGateId": observed_gate_id,
        "observedGateReasonCodes": observed_reasons or [],
    }


def deterministic_case_data() -> dict[str, object]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "evalId": "eval-safety-1",
        "evaluationMode": "deterministic",
        "checks": [
            check_data(
                "safety_blocking",
                observed_gate_id="G3",
                observed_reasons=["G3_INJURY_OR_EMERGENCY"],
            )
        ],
        "providerFailure": None,
        "providerCallCount": 0,
        "deterministicPassed": True,
        "passed": True,
        "durationMs": 3,
    }


def metrics_for(case: EvalCaseResult) -> list[dict[str, object]]:
    checked = {check.metric_id for check in case.checks}
    return [
        {
            "contractVersion": CONTRACT_VERSION,
            "metricId": metric.value,
            "status": "passed" if metric in checked else "not_applicable",
            "numerator": 1 if metric in checked else 0,
            "denominator": 1 if metric in checked else 0,
            "score": 1.0 if metric in checked else None,
        }
        for metric in EvalMetricId
    ]


def run_data() -> dict[str, object]:
    case = EvalCaseResult.model_validate(deterministic_case_data())
    return {
        "contractVersion": CONTRACT_VERSION,
        "runId": "eval-run-1",
        "datasetVersion": "eval-v1",
        "datasetSha256": "a" * 64,
        "commitSha": "b" * 40,
        "evaluationMode": "deterministic",
        "startedAt": "2026-07-14T12:00:00Z",
        "completedAt": "2026-07-14T12:00:01Z",
        "caseResults": [case],
        "metrics": metrics_for(case),
        "providerCallCount": 0,
        "totalCases": 1,
        "passedCases": 1,
        "failedCases": 0,
        "deterministicPassed": True,
        "passed": True,
    }


def test_eval_summary_derives_all_metrics_and_allows_passed_safety_block_check() -> None:
    run = EvalRunSummary.model_validate(run_data())
    safety = run.case_results[0].checks[0]
    assert safety.passed and safety.observed_gate_reason_codes == (
        GateReasonCode.G3_INJURY_OR_EMERGENCY,
    )
    assert len(run.metrics) == 10


def test_deterministic_eval_rejects_provider_model_and_invented_aggregates() -> None:
    case = deterministic_case_data()
    case["providerCallCount"] = 1
    with pytest.raises(ValidationError, match="provider"):
        EvalCaseResult.model_validate(case)

    case = deterministic_case_data()
    model_check = check_data("schema_validity")
    model_check["graderType"] = "model"
    model_check["score"] = 1.0
    case["checks"] = [model_check]
    with pytest.raises(ValidationError, match="deterministic"):
        EvalCaseResult.model_validate(case)

    invented = run_data()
    invented_metrics = cast(list[dict[str, object]], invented["metrics"])
    invented_metrics[0]["status"] = "passed"
    invented_metrics[0]["numerator"] = 1
    invented_metrics[0]["denominator"] = 1
    invented_metrics[0]["score"] = 1.0
    with pytest.raises(ValidationError, match="derived"):
        EvalRunSummary.model_validate(invented)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("datasetSha256", "not-a-digest"),
        ("commitSha", "not-a-commit"),
    ],
)
def test_eval_summary_rejects_invalid_source_identity(field: str, value: str) -> None:
    data = run_data()
    data[field] = value
    with pytest.raises(ValidationError):
        EvalRunSummary.model_validate(data)


def test_eval_summary_rejects_wrong_metric_order() -> None:
    data = run_data()
    metrics = cast(list[dict[str, object]], data["metrics"])
    metrics[0], metrics[1] = metrics[1], metrics[0]
    with pytest.raises(ValidationError, match="canonical order"):
        EvalRunSummary.model_validate(data)
