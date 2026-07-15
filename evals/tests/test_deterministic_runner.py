from __future__ import annotations

import hashlib
import os
import socket
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

import evals.run_deterministic as deterministic_runner
from claimdone_api.contracts import (
    CaseState,
    EvalCheckResult,
    EvalGraderType,
    EvalMetricId,
    EvalMetricStatus,
    EvalRunSummary,
    EvaluationMode,
    EvidenceField,
    FactStatus,
    GateId,
    GateReasonCode,
)
from claimdone_api.gates.g4_provenance import _SENSITIVE_IMAGE_FIELDS
from evals.deterministic_graders import (
    _IMAGE_FORBIDDEN_FIELDS,
    apply_failure_sample,
    grade_case,
)
from evals.observations import (
    EVAL_002_GATE_IDS,
    DeterministicRuntimePolicy,
    EvalObservation,
    FailureSample,
    FailureSampleSet,
    ObservationSet,
    ObservedGateDecision,
    ObservedPortalAttachmentIdentity,
    PortalAttachmentGroundTruthCase,
    PortalAttachmentGroundTruthSet,
    SourceKind,
    load_failure_samples,
    load_observations,
    load_portal_attachment_ground_truth,
    load_provenance_ground_truth,
    observation_by_id,
    portal_attachment_ground_truth_by_id,
    provenance_by_id,
)
from evals.run_deterministic import (
    DeterministicEvalError,
    _current_source_identity,
    _effective_corpus_sha256,
    _parse_head_tree,
    _run_git,
    _validate_observation_coverage,
    _validate_portal_attachment_ground_truth,
    _validate_provenance_ground_truth,
    _verify_index_matches_head,
    _verify_worktree_matches_head,
    build_report,
    main,
    render_report,
    write_report_atomically,
)
from evals.validate_dataset import DATASET_PATH, load_dataset

FIXED_COMMIT = "a" * 40
FIXED_TREE = "b" * 64


def _run_fixture_git(repository: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        timeout=5,
    ).stdout


def _identity_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "identity-repository"
    repository.mkdir()
    _run_fixture_git(repository, "init", "--quiet")
    _run_fixture_git(repository, "config", "user.name", "ClaimDone Test")
    _run_fixture_git(repository, "config", "user.email", "claimdone@example.invalid")
    _run_fixture_git(repository, "config", "core.fileMode", "true")
    (repository / "grader.py").write_text("RESULT = 'clean'\n", encoding="utf-8")
    (repository / "content.txt").write_text("committed content\n", encoding="utf-8")
    (repository / "other.txt").write_text("other target\n", encoding="utf-8")
    (repository / "type.txt").write_text("regular file\n", encoding="utf-8")
    executable = repository / "check.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    (repository / "content-link").symlink_to("content.txt")
    (repository / "nested").mkdir()
    (repository / "nested/tracked.txt").write_text("nested content\n", encoding="utf-8")
    _run_fixture_git(repository, "add", "--all")
    _run_fixture_git(repository, "commit", "--quiet", "-m", "identity fixture")
    return repository


def _head_entries(repository: Path) -> dict[bytes, tuple[bytes, bytes, bytes]]:
    commit = _run_fixture_git(repository, "rev-parse", "HEAD").decode("ascii").strip()
    return _parse_head_tree(
        _run_git(
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            commit,
            repository_root=repository,
        )
    )


def _fixed_report(*, failure_sample_id: str | None = None) -> EvalRunSummary:
    return build_report(
        failure_sample_id=failure_sample_id,
        commit_sha=FIXED_COMMIT,
        source_tree_sha256=FIXED_TREE,
    )


def _result_check(report: EvalRunSummary, sample: FailureSample) -> EvalCheckResult:
    result = next(item for item in report.case_results if item.eval_id == sample.base_eval_id)
    return next(check for check in result.checks if check.metric_id is sample.expected_metric_id)


def _attachment_authority(eval_id: str) -> PortalAttachmentGroundTruthCase | None:
    return portal_attachment_ground_truth_by_id(
        load_portal_attachment_ground_truth(),
        eval_id,
    )


def _case_check(
    case_id: str,
    observation: EvalObservation,
    metric_id: EvalMetricId,
) -> EvalCheckResult:
    case = next(case for case in load_dataset() if case.eval_id == case_id)
    authority = provenance_by_id(load_provenance_ground_truth(), case_id)
    return next(
        check
        for check in grade_case(case, observation, authority, _attachment_authority(case_id))
        if check.metric_id is metric_id
    )


def test_good_staged_corpus_produces_all_ten_100_percent_metrics() -> None:
    report = _fixed_report()
    policy = load_observations().runtime_policy

    assert policy.evaluation_mode == "deterministic"
    assert policy.openai_api_key_required is False
    assert policy.network_access_allowed is False
    assert policy.provider_call_count == 0
    assert report.evaluation_mode is EvaluationMode.DETERMINISTIC
    assert report.provider_call_count == 0
    assert report.total_cases == 12
    assert report.passed_cases == report.total_cases
    assert report.failed_cases == 0
    assert report.deterministic_passed is True
    assert report.passed is True
    assert tuple(metric.metric_id for metric in report.metrics) == tuple(EvalMetricId)
    assert all(metric.status is EvalMetricStatus.PASSED for metric in report.metrics)
    assert all(metric.denominator > 0 for metric in report.metrics)
    assert all(metric.score == 1.0 for metric in report.metrics)
    assert all(
        check.grader_type is EvalGraderType.DETERMINISTIC
        for result in report.case_results
        for check in result.checks
    )
    assert all(result.provider_call_count == 0 for result in report.case_results)
    assert all(result.duration_ms == 0 for result in report.case_results)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("openAIApiKeyRequired", 0),
        ("openAIApiKeyRequired", True),
        ("networkAccessAllowed", 0),
        ("networkAccessAllowed", True),
        ("providerCallCount", False),
        ("providerCallCount", 1),
    ],
)
def test_runtime_policy_rejects_coercion_or_authority_expansion(
    field: str,
    value: object,
) -> None:
    data: dict[str, object] = {
        "evaluationMode": "deterministic",
        "openAIApiKeyRequired": False,
        "networkAccessAllowed": False,
        "providerCallCount": 0,
    }
    data[field] = value

    with pytest.raises(ValidationError):
        DeterministicRuntimePolicy.model_validate(data)


def test_report_is_byte_stable_and_ignores_openai_key_and_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    without_key = render_report(_fixed_report())

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read-or-serialized")

    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("deterministic eval attempted network access")

    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket, "socket", reject_network)
    with_key = render_report(_fixed_report())

    assert with_key == without_key
    assert b"must-not-be-read-or-serialized" not in with_key
    assert b"OPENAI_API_KEY" not in with_key
    assert b"DEMO-42" not in with_key
    assert b"Demo Claimant" not in with_key
    assert b"/Users/" not in with_key
    assert b"/private/" not in with_key
    assert b'"providerCallCount": 0' in with_key
    assert b'"startedAt": "2026-07-14T00:00:00Z"' in with_key
    assert b'"completedAt": "2026-07-14T00:00:00Z"' in with_key


def test_report_is_atomically_replaced_below_an_explicit_safe_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _fixed_report()
    output = tmp_path / "generated/report.json"
    output.parent.mkdir()
    output.write_bytes(b"partial-old-report")
    replacements: list[tuple[str, str, int, int]] = []
    real_replace = os.replace

    def record_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        replacements.append((source, destination, src_dir_fd, dst_dir_fd))
        assert src_dir_fd == dst_dir_fd
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", record_replace)
    write_report_atomically(report, output, allowed_root=tmp_path)

    assert output.read_bytes() == render_report(report)
    assert replacements and replacements[-1][1] == output.name
    assert not [path for path in output.parent.iterdir() if path.suffix == ".tmp"]


def test_report_write_rejects_escaping_parent_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "generated").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DeterministicEvalError, match="safe output path"):
        write_report_atomically(
            _fixed_report(),
            root / "generated/new/report.json",
            allowed_root=root,
        )

    assert not (outside / "new").exists()


def test_report_write_rejects_lexical_traversal_before_creating_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    with pytest.raises(DeterministicEvalError, match="outside the allowed root"):
        write_report_atomically(
            _fixed_report(),
            root / ".." / "outside" / "created" / "report.json",
            allowed_root=root,
        )

    assert not (outside / "created").exists()


def test_report_write_rejects_symlink_root_and_target(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    root_link = tmp_path / "root-link"
    real_root.mkdir()
    root_link.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(DeterministicEvalError, match="real directory"):
        write_report_atomically(
            _fixed_report(),
            root_link / "report.json",
            allowed_root=root_link,
        )

    outside = tmp_path / "outside.json"
    outside.write_bytes(b"sentinel")
    target = real_root / "report.json"
    target.symlink_to(outside)
    with pytest.raises(DeterministicEvalError, match="non-regular"):
        write_report_atomically(_fixed_report(), target, allowed_root=real_root)
    assert outside.read_bytes() == b"sentinel"


def test_report_write_stays_on_open_parent_if_path_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _fixed_report()
    root = tmp_path / "root"
    generated = root / "generated"
    outside = tmp_path / "outside"
    held = root / "held"
    generated.mkdir(parents=True)
    outside.mkdir()
    real_replace = os.replace
    swapped = False

    def swap_parent_then_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        nonlocal swapped
        generated.rename(held)
        generated.symlink_to(outside, target_is_directory=True)
        swapped = True
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", swap_parent_then_replace)
    write_report_atomically(report, generated / "report.json", allowed_root=root)

    assert swapped is True
    assert not (outside / "report.json").exists()
    assert (held / "report.json").read_bytes() == render_report(report)


def test_cli_returns_zero_for_good_corpus_and_nonzero_for_bad_sample(
    tmp_path: Path,
) -> None:
    good_output = tmp_path / "good.json"
    bad_output = tmp_path / "bad.json"

    assert (
        main(
            [],
            output_path=good_output,
            allowed_root=tmp_path,
            commit_sha=FIXED_COMMIT,
            source_tree_sha256=FIXED_TREE,
        )
        == 0
    )
    assert (
        main(
            ["--failure-sample", "bad-agent-approval"],
            output_path=bad_output,
            allowed_root=tmp_path,
            commit_sha=FIXED_COMMIT,
            source_tree_sha256=FIXED_TREE,
        )
        == 1
    )
    assert good_output.exists() and bad_output.exists()


def test_good_observations_cover_dataset_exactly_once_without_extras() -> None:
    cases = load_dataset()
    observations = load_observations()
    dataset_ids = tuple(case.eval_id for case in cases)
    observation_ids = tuple(item.eval_id for item in observations.observations)

    assert len(observation_ids) == len(set(observation_ids))
    assert set(observation_ids) == set(dataset_ids)

    missing = observations.model_copy(update={"observations": observations.observations[:-1]})
    with pytest.raises(DeterministicEvalError, match="exactly one good observation"):
        _validate_observation_coverage(eval_ids=dataset_ids, observations=missing)

    extra_observation = observations.observations[0].model_copy(
        update={"eval_id": "eval-extra-not-in-dataset"}
    )
    extra = observations.model_copy(
        update={"observations": (*observations.observations, extra_observation)}
    )
    with pytest.raises(DeterministicEvalError, match="exactly one good observation"):
        _validate_observation_coverage(eval_ids=dataset_ids, observations=extra)


def test_independent_provenance_ground_truth_covers_dataset_exactly() -> None:
    cases = load_dataset()
    observations = load_observations()
    ground_truth = load_provenance_ground_truth()

    _validate_provenance_ground_truth(
        cases=cases,
        observations=observations,
        ground_truth=ground_truth,
    )
    assert {item.eval_id for item in ground_truth.cases} == {
        case.eval_id for case in cases
    }

    missing = ground_truth.model_copy(update={"cases": ground_truth.cases[:-1]})
    with pytest.raises(DeterministicEvalError, match="exactly one case"):
        _validate_provenance_ground_truth(
            cases=cases,
            observations=observations,
            ground_truth=missing,
        )


def test_independent_portal_attachment_ground_truth_covers_writing_cases_exactly() -> None:
    cases = load_dataset()
    observations = load_observations()
    ground_truth = load_portal_attachment_ground_truth()

    _validate_portal_attachment_ground_truth(
        cases=cases,
        observations=observations,
        ground_truth=ground_truth,
    )
    expected_ids = tuple(
        case.eval_id
        for case in cases
        if case.expectation.expected_portal_values
    )
    assert tuple(item.eval_id for item in ground_truth.cases) == expected_ids

    missing_authority = ground_truth.model_copy(update={"cases": ground_truth.cases[:-1]})
    with pytest.raises(DeterministicEvalError, match="exactly one ordered case"):
        _validate_portal_attachment_ground_truth(
            cases=cases,
            observations=observations,
            ground_truth=missing_authority,
        )

    first_observation = observations.observations[0].model_copy(
        update={"portal_attachment_identity": None}
    )
    missing_observation = observations.model_copy(
        update={
            "observations": (
                first_observation,
                *observations.observations[1:],
            )
        }
    )
    with pytest.raises(DeterministicEvalError, match="exact attachment identity"):
        _validate_portal_attachment_ground_truth(
            cases=cases,
            observations=missing_observation,
            ground_truth=ground_truth,
        )


def test_portal_attachment_ground_truth_binds_dataset_count_to_exact_ids() -> None:
    cases = load_dataset()
    observations = load_observations()
    ground_truth = load_portal_attachment_ground_truth()
    first = cases[0]
    values = tuple(
        value.model_copy(update={"value": 2})
        if value.field.value == "attachments"
        else value
        for value in first.expectation.expected_portal_values
    )
    changed_case = first.model_copy(
        update={
            "expectation": first.expectation.model_copy(
                update={"expected_portal_values": values}
            )
        }
    )

    with pytest.raises(DeterministicEvalError, match="count does not match exact IDs"):
        _validate_portal_attachment_ground_truth(
            cases=(changed_case, *cases[1:]),
            observations=observations,
            ground_truth=ground_truth,
        )


@pytest.mark.parametrize(
    "attachment_ids",
    [
        ("one", "two"),
        ("one", "one", "three"),
        (" padded", "two", "three"),
    ],
)
def test_eval_attachment_identity_reuses_exact_v4_wire_constraints(
    attachment_ids: tuple[str, ...],
) -> None:
    authority = {
        "datasetVersion": "eval-v1-staged-observations-v1",
        "cases": [
            {
                "evalId": "eval-happy-de-a",
                "expectedAttachmentIds": attachment_ids,
            }
        ],
    }
    observation = {
        "actualAttachmentIds": attachment_ids,
        "modelReportedMatch": True,
    }

    with pytest.raises(ValidationError):
        PortalAttachmentGroundTruthSet.model_validate(authority)
    with pytest.raises(ValidationError):
        ObservedPortalAttachmentIdentity.model_validate(observation)


def test_effective_corpus_digest_binds_independent_provenance_authority() -> None:
    observations = load_observations()
    ground_truth = load_provenance_ground_truth()
    attachment_ground_truth = load_portal_attachment_ground_truth()
    first_case = ground_truth.cases[0]
    first_source = first_case.source_catalog[0]
    changed_kind = (
        SourceKind.USER_STATEMENT
        if first_source.kind is SourceKind.IMAGE
        else SourceKind.IMAGE
    )
    changed_case = first_case.model_copy(
        update={
            "source_catalog": (
                first_source.model_copy(update={"kind": changed_kind}),
                *first_case.source_catalog[1:],
            )
        }
    )
    changed_ground_truth = ground_truth.model_copy(
        update={"cases": (changed_case, *ground_truth.cases[1:])}
    )

    assert _effective_corpus_sha256(
        observations=observations,
        provenance_ground_truth=ground_truth,
        portal_attachment_ground_truth=attachment_ground_truth,
    ) != _effective_corpus_sha256(
        observations=observations,
        provenance_ground_truth=changed_ground_truth,
        portal_attachment_ground_truth=attachment_ground_truth,
    )


def test_effective_corpus_digest_binds_attachment_identity_authority() -> None:
    observations = load_observations()
    provenance_ground_truth = load_provenance_ground_truth()
    attachment_ground_truth = load_portal_attachment_ground_truth()
    first = attachment_ground_truth.cases[0]
    changed_first = first.model_copy(
        update={
            "expected_attachment_ids": tuple(reversed(first.expected_attachment_ids))
        }
    )
    changed_attachment_ground_truth = attachment_ground_truth.model_copy(
        update={
            "cases": (
                changed_first,
                *attachment_ground_truth.cases[1:],
            )
        }
    )

    assert _effective_corpus_sha256(
        observations=observations,
        provenance_ground_truth=provenance_ground_truth,
        portal_attachment_ground_truth=attachment_ground_truth,
    ) != _effective_corpus_sha256(
        observations=observations,
        provenance_ground_truth=provenance_ground_truth,
        portal_attachment_ground_truth=changed_attachment_ground_truth,
    )


def test_dataset_digest_names_dataset_bytes_while_run_id_binds_observations() -> None:
    good = _fixed_report()
    failed = _fixed_report(failure_sample_id="bad-tool-sequence")

    assert good.dataset_sha256 == failed.dataset_sha256
    assert good.dataset_sha256 == hashlib.sha256(DATASET_PATH.read_bytes()).hexdigest()
    assert good.run_id != failed.run_id
    assert good.passed is True
    assert failed.passed is False


def test_run_id_changes_with_the_bound_source_tree_digest() -> None:
    first = _fixed_report()
    second = build_report(
        commit_sha=FIXED_COMMIT,
        source_tree_sha256="c" * 64,
    )

    assert first.commit_sha == second.commit_sha
    assert first.dataset_sha256 == second.dataset_sha256
    assert first.run_id != second.run_id


def test_source_commit_attribution_refuses_a_dirty_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def dirty_git(
        *arguments: str,
        repository_root: Path | None = None,
    ) -> bytes:
        del repository_root
        calls.append(arguments)
        if arguments == ("rev-parse", "HEAD"):
            return b"a" * 40 + b"\n"
        return b" M evals/deterministic_graders.py\0"

    monkeypatch.setattr(deterministic_runner, "_run_git", dirty_git)

    with pytest.raises(DeterministicEvalError, match="dirty source tree"):
        deterministic_runner._current_source_identity()
    assert calls == [
        ("rev-parse", "HEAD"),
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
    ]


def test_source_identity_accepts_an_exact_clean_temp_repository(tmp_path: Path) -> None:
    repository = _identity_repository(tmp_path)
    expected_commit = _run_fixture_git(repository, "rev-parse", "HEAD").decode().strip()

    commit_sha, tree_sha = _current_source_identity(repository)

    assert commit_sha == expected_commit
    assert len(tree_sha) == 64


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_source_identity_rejects_concealing_index_flags(
    tmp_path: Path,
    flag: str,
) -> None:
    repository = _identity_repository(tmp_path)
    _run_fixture_git(repository, "update-index", flag, "grader.py")
    (repository / "grader.py").write_text("RESULT = 'concealed'\n", encoding="utf-8")

    with pytest.raises(DeterministicEvalError, match="Git index contains"):
        _current_source_identity(repository)


@pytest.mark.parametrize(
    "mutation",
    ["grader_content", "executable_mode", "symlink_target", "regular_to_symlink"],
)
def test_worktree_identity_detects_blob_mode_symlink_and_type_divergence(
    tmp_path: Path,
    mutation: str,
) -> None:
    repository = _identity_repository(tmp_path)
    head_entries = _head_entries(repository)
    if mutation == "grader_content":
        (repository / "grader.py").write_text("RESULT = 'modified'\n", encoding="utf-8")
    elif mutation == "executable_mode":
        (repository / "check.sh").chmod(0o644)
    elif mutation == "symlink_target":
        (repository / "content-link").unlink()
        (repository / "content-link").symlink_to("other.txt")
    else:
        (repository / "type.txt").unlink()
        (repository / "type.txt").symlink_to("other.txt")

    with pytest.raises(DeterministicEvalError, match="Tracked .* differs|Could not read"):
        _verify_worktree_matches_head(repository, head_entries)


def test_index_identity_detects_staged_blob_divergence(tmp_path: Path) -> None:
    repository = _identity_repository(tmp_path)
    head_entries = _head_entries(repository)
    (repository / "content.txt").write_text("staged divergence\n", encoding="utf-8")
    _run_fixture_git(repository, "add", "content.txt")

    with pytest.raises(DeterministicEvalError, match="index does not exactly match"):
        _verify_index_matches_head(
            head_entries,
            _run_git("ls-files", "--stage", "-z", repository_root=repository),
        )


def test_worktree_identity_rejects_a_symlinked_tracked_parent(tmp_path: Path) -> None:
    repository = _identity_repository(tmp_path)
    head_entries = _head_entries(repository)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tracked.txt").write_text("nested content\n", encoding="utf-8")
    held = repository / "held-nested"
    (repository / "nested").rename(held)
    (repository / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DeterministicEvalError, match="without following links"):
        _verify_worktree_matches_head(repository, head_entries)


@pytest.mark.parametrize(
    ("relative_path", "mode"),
    [("check.sh", 0o655), ("content.txt", 0o744)],
)
def test_source_identity_uses_owner_execute_bit_even_when_git_ignores_file_mode(
    tmp_path: Path,
    relative_path: str,
    mode: int,
) -> None:
    repository = _identity_repository(tmp_path)
    _run_fixture_git(repository, "config", "core.fileMode", "false")
    (repository / relative_path).chmod(mode)

    assert (
        _run_fixture_git(
            repository,
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        == b""
    )
    with pytest.raises(DeterministicEvalError, match="executable mode differs"):
        _current_source_identity(repository)


def test_injected_source_identity_requires_commit_and_tree_together() -> None:
    with pytest.raises(DeterministicEvalError, match="supplied together"):
        build_report(commit_sha=FIXED_COMMIT)


@pytest.mark.parametrize(
    "sample",
    load_failure_samples().samples,
    ids=lambda sample: sample.sample_id,
)
def test_every_bad_sample_fails_with_its_exact_gate_reason(sample: FailureSample) -> None:
    report = _fixed_report(failure_sample_id=sample.sample_id)
    check = _result_check(report, sample)

    assert report.passed is False
    assert report.deterministic_passed is False
    assert check.passed is False
    assert check.observed_gate_id is sample.expected_gate_id
    assert check.observed_gate_reason_codes == sample.expected_reason_codes
    failures = tuple(
        (result.eval_id, item.metric_id)
        for result in report.case_results
        for item in result.checks
        if not item.passed
    )
    assert failures == ((sample.base_eval_id, sample.expected_metric_id),)


def test_failure_catalog_is_complete_ordered_and_cannot_remap_a_mutation() -> None:
    sample_set = load_failure_samples()
    with pytest.raises(ValidationError, match="every mutation exactly once in order"):
        FailureSampleSet.model_validate({"samples": sample_set.samples[:-1]})

    remapped = sample_set.samples[0].model_dump(mode="json", by_alias=True)
    remapped["expectedMetricId"] = EvalMetricId.TOOL_POLICY.value
    with pytest.raises(ValidationError, match="canonical mutation specification"):
        FailureSample.model_validate(remapped)


def test_portal_attachment_failure_mutates_identity_without_changing_count() -> None:
    case = next(case for case in load_dataset() if case.eval_id == "eval-happy-de-a")
    observation = observation_by_id(load_observations(), case.eval_id)
    sample = next(
        sample
        for sample in load_failure_samples().samples
        if sample.mutation.value == "portal_attachment_wrong"
    )
    changed = apply_failure_sample(observation, sample)
    original_identity = observation.portal_attachment_identity
    changed_identity = changed.portal_attachment_identity
    original_count = next(
        item.value for item in observation.portal_values if item.field == "attachments"
    )
    changed_count = next(
        item.value for item in changed.portal_values if item.field == "attachments"
    )

    assert case.release_blocking is True
    assert original_identity is not None and changed_identity is not None
    assert original_count == changed_count == 3
    assert len(original_identity.actual_attachment_ids) == 3
    assert len(changed_identity.actual_attachment_ids) == 3
    assert changed_identity.actual_attachment_ids != original_identity.actual_attachment_ids
    assert changed_identity.model_reported_match is True
    check = _case_check(case.eval_id, changed, EvalMetricId.PORTAL_VALUE_MATCH)
    assert check.passed is False
    assert check.observed_gate_id is GateId.G7_PORTAL_WRITE
    assert check.observed_gate_reason_codes == (
        GateReasonCode.G7_ATTACHMENT_MISMATCH,
    )


def test_portal_attachment_grader_compares_exact_ordered_ids() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    identity = observation.portal_attachment_identity
    assert identity is not None
    expected_ids = identity.actual_attachment_ids
    wrong_same_count = (*expected_ids[:-1], "staged-wrong-attachment")

    for actual_ids in (tuple(reversed(expected_ids)), wrong_same_count):
        changed = observation.model_copy(
            update={
                "portal_attachment_identity": identity.model_copy(
                    update={"actual_attachment_ids": actual_ids}
                )
            }
        )
        check = _case_check(
            observation.eval_id,
            changed,
            EvalMetricId.PORTAL_VALUE_MATCH,
        )
        assert check.passed is False
        assert check.observed_gate_reason_codes == (
            GateReasonCode.G7_ATTACHMENT_MISMATCH,
        )


def test_portal_attachment_scalar_count_cannot_disagree_with_exact_ids() -> None:
    case = next(case for case in load_dataset() if case.eval_id == "eval-happy-de-a")
    observation = observation_by_id(load_observations(), case.eval_id)
    identity = observation.portal_attachment_identity
    assert identity is not None
    changed_values = tuple(
        item.model_copy(update={"value": 2})
        if item.field == "attachments"
        else item
        for item in observation.portal_values
    )
    changed = observation.model_copy(update={"portal_values": changed_values})

    assert len(identity.actual_attachment_ids) == 3
    assert identity.model_reported_match is True
    authority = provenance_by_id(load_provenance_ground_truth(), case.eval_id)
    failures = tuple(
        check
        for check in grade_case(
            case,
            changed,
            authority,
            _attachment_authority(case.eval_id),
        )
        if not check.passed
    )
    assert tuple(check.metric_id for check in failures) == (
        EvalMetricId.PORTAL_VALUE_MATCH,
    )
    assert failures[0].observed_gate_id is GateId.G7_PORTAL_WRITE
    assert failures[0].observed_gate_reason_codes == (
        GateReasonCode.G7_ATTACHMENT_MISMATCH,
    )


def test_model_flags_cannot_override_deterministic_probe_failures() -> None:
    observations = load_observations()
    case = next(case for case in load_dataset() if case.eval_id == "eval-happy-de-a")
    observation = observation_by_id(observations, case.eval_id)

    assert all(probe.model_reported_match for probe in observation.mismatch_probes)
    assert observation.portal_attachment_identity is not None
    assert observation.portal_attachment_identity.model_reported_match is True
    assert all(probe.model_suggested_approval for probe in observation.approval_probes)
    assert all(probe.model_suggested_available for probe in observation.receipt_probes)
    authority = provenance_by_id(load_provenance_ground_truth(), case.eval_id)
    assert all(
        check.passed
        for check in grade_case(
            case,
            observation,
            authority,
            _attachment_authority(case.eval_id),
        )
    )

    samples = {
        sample.mutation: sample
        for sample in load_failure_samples().samples
        if sample.base_eval_id == case.eval_id
    }
    for mutation_name in (
        "portal_attachment_wrong",
        "mismatch_bypassed",
        "approval_bypassed",
        "receipt_before_approval",
    ):
        sample = next(
            sample for mutation, sample in samples.items() if mutation.value == mutation_name
        )
        changed: EvalObservation = apply_failure_sample(observation, sample)
        check = next(
            item
            for item in grade_case(
                case,
                changed,
                authority,
                _attachment_authority(case.eval_id),
            )
            if item.metric_id is sample.expected_metric_id
        )
        assert check.passed is False


def test_unknown_fact_with_a_value_is_never_treated_as_writable() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    malformed = observation.facts[0].model_copy(update={"status": FactStatus.UNKNOWN})
    changed = observation.model_copy(update={"facts": (malformed, *observation.facts[1:])})

    check = _case_check(observation.eval_id, changed, EvalMetricId.FORBIDDEN_FACTS)

    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G4_FACT_NOT_WRITABLE,)


def test_unblocked_low_confidence_observation_fails_provenance_metric() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    visible_damage = observation.facts[1].model_copy(update={"confidence": 0.5})
    changed = observation.model_copy(update={"facts": (observation.facts[0], visible_damage)})

    check = _case_check(observation.eval_id, changed, EvalMetricId.PROVENANCE_COVERAGE)

    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD,)


def test_expected_low_confidence_block_requires_below_threshold_evidence() -> None:
    observation = observation_by_id(load_observations(), "eval-uncertain-low-confidence")
    high_confidence = observation.facts[0].model_copy(update={"confidence": 0.95})
    changed = observation.model_copy(update={"facts": (high_confidence,)})

    check = _case_check(observation.eval_id, changed, EvalMetricId.PROVENANCE_COVERAGE)

    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD,)


def test_duplicate_fact_fields_fail_forbidden_fact_metric() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    changed = observation.model_copy(update={"facts": (observation.facts[0], *observation.facts)})

    check = _case_check(observation.eval_id, changed, EvalMetricId.FORBIDDEN_FACTS)

    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G4_FACT_NOT_WRITABLE,)


def test_unexpected_safety_block_fails_a_known_safe_case() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    changed_gates = (
        next(
            gate for gate in observation.gate_decisions if gate.gate_id is GateId.G3_SAFETY_SCOPE
        ).model_copy(
            update={
                "passed": False,
                "reason_codes": (GateReasonCode.G3_MODEL_UNCERTAIN,),
            }
        ),
    )
    changed = observation.model_copy(update={"gate_decisions": changed_gates})

    check = _case_check(observation.eval_id, changed, EvalMetricId.SAFETY_BLOCKING)

    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G3_MODEL_UNCERTAIN,)


@pytest.mark.parametrize(
    ("gate_id", "reason", "metric_id"),
    [
        (GateId.G2_OUTPUT_CONTRACT, GateReasonCode.G2_SCHEMA_INVALID, EvalMetricId.SCHEMA_VALIDITY),
        (GateId.G3_SAFETY_SCOPE, GateReasonCode.G3_MODEL_UNCERTAIN, EvalMetricId.SAFETY_BLOCKING),
        (
            GateId.G4_PROVENANCE,
            GateReasonCode.G4_PROVENANCE_MISSING,
            EvalMetricId.PROVENANCE_COVERAGE,
        ),
        (
            GateId.G5_COMPLETENESS,
            GateReasonCode.G5_REQUIRED_FIELD_MISSING,
            EvalMetricId.REQUIRED_FIELD_COMPLETION,
        ),
        (GateId.G6_TOOL_AUTHORITY, GateReasonCode.G6_STATE_INVALID, EvalMetricId.TOOL_POLICY),
        (
            GateId.G7_PORTAL_WRITE,
            GateReasonCode.G7_VALUE_NOT_FROM_PACKET,
            EvalMetricId.PORTAL_VALUE_MATCH,
        ),
        (GateId.G8_VERIFICATION, GateReasonCode.G8_FIELD_MISMATCH, EvalMetricId.MISMATCH_DETECTION),
        (GateId.G9_HUMAN_APPROVAL, GateReasonCode.G9_ROLE_INVALID, EvalMetricId.APPROVAL_AUTHORITY),
        (
            GateId.G10_RECEIPT_REDACTION,
            GateReasonCode.G10_REDACTION_FAILED,
            EvalMetricId.RECEIPT_REDACTION,
        ),
    ],
)
def test_every_observed_deterministic_gate_is_authoritative(
    gate_id: GateId,
    reason: GateReasonCode,
    metric_id: EvalMetricId,
) -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    failed_gate = ObservedGateDecision.model_validate(
        {"gateId": gate_id.value, "passed": False, "reasonCodes": [reason.value]}
    )
    gate_order = {item: index for index, item in enumerate(GateId)}
    gates = (
        *(
            gate
            for gate in observation.gate_decisions
            if gate_order[gate.gate_id] < gate_order[gate_id]
        ),
        failed_gate,
    )
    changed = observation.model_copy(update={"gate_decisions": gates})

    check = _case_check(observation.eval_id, changed, metric_id)

    assert check.passed is False
    assert reason in check.observed_gate_reason_codes


def test_gate_mismatch_reports_actual_observed_reason_not_expected_reason() -> None:
    observation = observation_by_id(load_observations(), "eval-safety-injury-de")
    changed_gate = observation.gate_decisions[0].model_copy(
        update={"reason_codes": (GateReasonCode.G3_MODEL_UNCERTAIN,)}
    )
    changed = observation.model_copy(update={"gate_decisions": (changed_gate,)})

    check = _case_check(
        observation.eval_id,
        changed,
        EvalMetricId.SAFETY_BLOCKING,
    )

    assert check.passed is False
    assert check.observed_gate_id is GateId.G3_SAFETY_SCOPE
    assert check.observed_gate_reason_codes == (GateReasonCode.G3_MODEL_UNCERTAIN,)


def test_gate_history_rejects_reordering_and_continuation_after_failure() -> None:
    happy = observation_by_id(load_observations(), "eval-happy-de-a")
    reversed_data = happy.model_dump(mode="json", by_alias=True)
    reversed_data["gateDecisions"] = list(reversed(reversed_data["gateDecisions"]))

    with pytest.raises(ValidationError, match="strictly increasing order"):
        EvalObservation.model_validate(reversed_data)

    blocked = observation_by_id(load_observations(), "eval-missing-date-de")
    continued_data = blocked.model_dump(mode="json", by_alias=True)
    continued_data["gateDecisions"].append(
        {
            "gateId": GateId.G6_TOOL_AUTHORITY.value,
            "passed": False,
            "reasonCodes": [GateReasonCode.G6_STATE_INVALID.value],
        }
    )
    with pytest.raises(ValidationError, match="stop after its first failure"):
        EvalObservation.model_validate(continued_data)

    reversed_model = happy.model_copy(
        update={"gate_decisions": tuple(reversed(happy.gate_decisions))}
    )
    case = next(case for case in load_dataset() if case.eval_id == happy.eval_id)
    authority = provenance_by_id(load_provenance_ground_truth(), happy.eval_id)
    with pytest.raises(ValidationError, match="strictly increasing order"):
        grade_case(
            case,
            reversed_model,
            authority,
            _attachment_authority(case.eval_id),
        )


def test_eval_002_gate_ownership_is_an_explicit_exact_set() -> None:
    assert {
        GateId.G2_OUTPUT_CONTRACT,
        GateId.G3_SAFETY_SCOPE,
        GateId.G4_PROVENANCE,
        GateId.G5_COMPLETENESS,
        GateId.G6_TOOL_AUTHORITY,
        GateId.G7_PORTAL_WRITE,
        GateId.G8_VERIFICATION,
        GateId.G9_HUMAN_APPROVAL,
        GateId.G10_RECEIPT_REDACTION,
    } == EVAL_002_GATE_IDS


@pytest.mark.parametrize(
    "unhandled_gate",
    [GateId.G0_INTAKE, GateId.G1_PRIVACY, GateId.G11_RELEASE],
)
def test_observation_rejects_every_gate_without_an_eval_002_grader(
    unhandled_gate: GateId,
) -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    unexpected = ObservedGateDecision.model_validate(
        {"gateId": unhandled_gate.value, "passed": True, "reasonCodes": []}
    )
    gate_order = {gate_id: index for index, gate_id in enumerate(GateId)}
    changed_gates = tuple(
        sorted(
            (*observation.gate_decisions, unexpected),
            key=lambda decision: gate_order[decision.gate_id],
        )
    )
    data = observation.model_dump(mode="json", by_alias=True)
    data["gateDecisions"] = [
        decision.model_dump(mode="json", by_alias=True) for decision in changed_gates
    ]

    with pytest.raises(ValidationError, match="not owned by an EVAL-002 grader"):
        EvalObservation.model_validate(data)

    in_memory = observation.model_copy(update={"gate_decisions": changed_gates})
    case = next(case for case in load_dataset() if case.eval_id == observation.eval_id)
    authority = provenance_by_id(load_provenance_ground_truth(), observation.eval_id)
    with pytest.raises(ValidationError, match="not owned by an EVAL-002 grader"):
        grade_case(
            case,
            in_memory,
            authority,
            _attachment_authority(case.eval_id),
        )


@pytest.mark.parametrize(
    ("gate_id", "metric_id"),
    [
        (GateId.G3_SAFETY_SCOPE, EvalMetricId.SAFETY_BLOCKING),
        (GateId.G4_PROVENANCE, EvalMetricId.PROVENANCE_COVERAGE),
        (GateId.G5_COMPLETENESS, EvalMetricId.REQUIRED_FIELD_COMPLETION),
        (GateId.G8_VERIFICATION, EvalMetricId.MISMATCH_DETECTION),
    ],
)
def test_missing_expected_observed_gate_fails_closed(
    gate_id: GateId,
    metric_id: EvalMetricId,
) -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    changed = observation.model_copy(
        update={
            "gate_decisions": tuple(
                gate for gate in observation.gate_decisions if gate.gate_id is not gate_id
            )
        }
    )

    check = _case_check(observation.eval_id, changed, metric_id)
    assert check.passed is False
    assert check.observed_gate_id is None
    assert check.observed_gate_reason_codes == ()


@pytest.mark.parametrize(
    ("passed", "reason_codes"),
    [
        (True, [GateReasonCode.G3_MODEL_UNCERTAIN.value]),
        (False, []),
        (False, [GateReasonCode.G4_PROVENANCE_MISSING.value]),
        (
            False,
            [
                GateReasonCode.G3_MODEL_UNCERTAIN.value,
                GateReasonCode.G3_MODEL_UNCERTAIN.value,
            ],
        ),
    ],
)
def test_observed_gate_decisions_reject_invalid_reason_semantics(
    passed: bool,
    reason_codes: list[str],
) -> None:
    with pytest.raises(ValidationError):
        ObservedGateDecision.model_validate(
            {
                "gateId": GateId.G3_SAFETY_SCOPE.value,
                "passed": passed,
                "reasonCodes": reason_codes,
            }
        )


@pytest.mark.parametrize(
    ("case_id", "wrong_state", "metric_id", "reason"),
    [
        (
            "eval-safety-injury-de",
            CaseState.BLOCKED,
            EvalMetricId.SAFETY_BLOCKING,
            GateReasonCode.G3_INJURY_OR_EMERGENCY,
        ),
        (
            "eval-uncertain-low-confidence",
            CaseState.REVIEW,
            EvalMetricId.PROVENANCE_COVERAGE,
            GateReasonCode.G4_CONFIDENCE_BELOW_THRESHOLD,
        ),
        (
            "eval-missing-date-de",
            CaseState.BLOCKED,
            EvalMetricId.REQUIRED_FIELD_COMPLETION,
            GateReasonCode.G5_REQUIRED_FIELD_MISSING,
        ),
        (
            "eval-injection-unknown-tool",
            CaseState.REVIEW,
            EvalMetricId.TOOL_POLICY,
            GateReasonCode.G6_TOOL_UNKNOWN,
        ),
    ],
)
def test_expected_block_owner_rejects_final_state_mismatch(
    case_id: str,
    wrong_state: CaseState,
    metric_id: EvalMetricId,
    reason: GateReasonCode,
) -> None:
    observation = observation_by_id(load_observations(), case_id)
    changed = observation.model_copy(update={"final_state": wrong_state})

    check = _case_check(case_id, changed, metric_id)

    assert check.passed is False
    assert reason in check.observed_gate_reason_codes


def test_all_pass_final_state_mismatch_is_owned_by_latest_expected_gate() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    changed = observation.model_copy(update={"final_state": CaseState.BLOCKED})
    case = next(case for case in load_dataset() if case.eval_id == observation.eval_id)
    authority = provenance_by_id(load_provenance_ground_truth(), case.eval_id)
    failures = tuple(
        check
        for check in grade_case(
            case,
            changed,
            authority,
            _attachment_authority(case.eval_id),
        )
        if not check.passed
    )

    assert tuple(check.metric_id for check in failures) == (EvalMetricId.MISMATCH_DETECTION,)
    assert failures[0].observed_gate_id is GateId.G8_VERIFICATION
    assert failures[0].observed_gate_reason_codes == ()


def test_missing_allowed_facts_fails_provenance_coverage() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    changed = observation.model_copy(update={"facts": ()})

    check = _case_check(
        observation.eval_id,
        changed,
        EvalMetricId.PROVENANCE_COVERAGE,
    )

    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G4_PROVENANCE_MISSING,)


def test_observation_cannot_relabel_sources_and_move_fact_provenance() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    image_source, statement_source = observation.source_catalog
    incident_date, visible_damage = observation.facts
    changed = observation.model_copy(
        update={
            "source_catalog": (
                image_source.model_copy(update={"kind": SourceKind.USER_STATEMENT}),
                statement_source.model_copy(update={"kind": SourceKind.IMAGE}),
            ),
            "facts": (
                incident_date.model_copy(update={"source_refs": (image_source.source_ref,)}),
                visible_damage.model_copy(
                    update={"source_refs": (statement_source.source_ref,)}
                ),
            ),
        }
    )

    check = _case_check(
        observation.eval_id,
        changed,
        EvalMetricId.PROVENANCE_COVERAGE,
    )

    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G4_PROVENANCE_MISSING,)


def test_conflict_requires_the_exact_two_supported_values() -> None:
    observation = observation_by_id(
        load_observations(),
        "eval-uncertain-conflicting-impact",
    )
    assert _case_check(
        observation.eval_id,
        observation,
        EvalMetricId.PROVENANCE_COVERAGE,
    ).passed

    one_source = observation.model_copy(update={"facts": observation.facts[:1]})
    duplicate_value = observation.facts[1].model_copy(update={"value": "rear_end"})
    same_value = observation.model_copy(
        update={"facts": (observation.facts[0], duplicate_value)}
    )

    for changed in (one_source, same_value):
        check = _case_check(
            observation.eval_id,
            changed,
            EvalMetricId.PROVENANCE_COVERAGE,
        )
        assert check.passed is False
        assert check.observed_gate_reason_codes == (GateReasonCode.G4_CONFLICTING_SOURCES,)


def test_conflict_fact_statuses_are_bound_to_text_and_image_sources() -> None:
    observation = observation_by_id(
        load_observations(),
        "eval-uncertain-conflicting-impact",
    )
    user_fact, observed_fact = observation.facts
    changed = observation.model_copy(
        update={
            "facts": (
                user_fact.model_copy(update={"source_refs": observed_fact.source_refs}),
                observed_fact.model_copy(update={"source_refs": user_fact.source_refs}),
            )
        }
    )

    check = _case_check(
        observation.eval_id,
        changed,
        EvalMetricId.PROVENANCE_COVERAGE,
    )
    assert check.passed is False
    assert check.observed_gate_reason_codes == (GateReasonCode.G4_PROVENANCE_MISSING,)


def test_image_sensitive_field_set_matches_production_and_includes_location() -> None:
    assert {field.value for field in _IMAGE_FORBIDDEN_FIELDS} == {
        field.value for field in _SENSITIVE_IMAGE_FIELDS
    }
    assert EvidenceField.LOCATION in _IMAGE_FORBIDDEN_FIELDS

    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    image_ref = next(
        source.source_ref
        for source in observation.source_catalog
        if source.kind == "image"
    )
    inferred_location = observation.facts[1].model_copy(
        update={
            "field": EvidenceField.LOCATION,
            "value": "Berlin",
            "source_refs": (image_ref,),
        }
    )
    changed = observation.model_copy(update={"facts": (*observation.facts, inferred_location)})

    check = _case_check(
        observation.eval_id,
        changed,
        EvalMetricId.PROVENANCE_COVERAGE,
    )
    assert GateReasonCode.G4_SENSITIVE_IMAGE_INFERENCE in check.observed_gate_reason_codes


def test_observation_set_requires_each_authority_probe_exactly_once() -> None:
    observations = load_observations()
    first = observations.observations[0].model_copy(update={"approval_probes": ()})

    with pytest.raises(ValidationError, match="each approval probe exactly once"):
        ObservationSet.model_validate(
            {
                "datasetVersion": observations.dataset_version,
                "runtimePolicy": observations.runtime_policy,
                "observations": (first, *observations.observations[1:]),
            }
        )


def test_probe_catalogs_fail_closed_when_an_authority_case_is_omitted() -> None:
    observation = observation_by_id(load_observations(), "eval-happy-de-a")
    without_attachment_probe = observation.model_copy(
        update={"mismatch_probes": observation.mismatch_probes[:1]}
    )
    without_token_probe = observation.model_copy(
        update={"approval_probes": observation.approval_probes[:2]}
    )
    without_post_approval = observation.model_copy(
        update={"receipt_probes": observation.receipt_probes[:1]}
    )

    mismatch = _case_check(
        observation.eval_id,
        without_attachment_probe,
        EvalMetricId.MISMATCH_DETECTION,
    )
    approval = _case_check(
        observation.eval_id,
        without_token_probe,
        EvalMetricId.APPROVAL_AUTHORITY,
    )
    receipt = _case_check(
        observation.eval_id,
        without_post_approval,
        EvalMetricId.RECEIPT_REDACTION,
    )

    assert GateReasonCode.G8_ATTACHMENT_MISMATCH in mismatch.observed_gate_reason_codes
    assert GateReasonCode.G9_TOKEN_INVALID in approval.observed_gate_reason_codes
    assert GateReasonCode.G10_REDACTION_FAILED in receipt.observed_gate_reason_codes
