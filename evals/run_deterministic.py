"""Run ClaimDone's staged deterministic eval corpus without providers or network."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from claimdone_api.contracts import (
    CONTRACT_VERSION,
    EvalCase,
    EvalCaseResult,
    EvalMetricAggregate,
    EvalMetricId,
    EvalMetricStatus,
    EvalRunSummary,
    EvaluationMode,
)
from evals.deterministic_graders import apply_failure_sample, grade_case
from evals.observations import (
    REPOSITORY_ROOT,
    EvalObservation,
    FailureSample,
    ObservationSet,
    ObservationValidationError,
    ProvenanceGroundTruthSet,
    load_failure_samples,
    load_observations,
    load_provenance_ground_truth,
    observation_by_id,
    provenance_by_id,
)
from evals.validate_dataset import DATASET_PATH, load_dataset

REPORT_PATH = REPOSITORY_ROOT / "evals/generated/deterministic-report.json"
_FIXED_EVAL_TIME = datetime(2026, 7, 14, tzinfo=UTC)


class DeterministicEvalError(ValueError):
    """The deterministic corpus is incomplete or an expected failure did not occur."""


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _dataset_value() -> object:
    try:
        return json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeterministicEvalError("Could not hash the deterministic dataset") from error


def _dataset_sha256() -> str:
    """Hash the exact versioned dataset artifact named by datasetSha256."""

    try:
        dataset_bytes = DATASET_PATH.read_bytes()
    except OSError as error:
        raise DeterministicEvalError("Could not hash the deterministic dataset") from error
    return hashlib.sha256(dataset_bytes).hexdigest()


def _effective_corpus_sha256(
    *,
    observations: ObservationSet,
    provenance_ground_truth: ProvenanceGroundTruthSet,
) -> str:
    """Bind the report to both ground truth and the exact staged observations."""

    corpus = {
        "dataset": _dataset_value(),
        "observations": observations.model_dump(mode="json", by_alias=True),
        "provenanceGroundTruth": provenance_ground_truth.model_dump(
            mode="json",
            by_alias=True,
        ),
    }
    return hashlib.sha256(_canonical_bytes(corpus)).hexdigest()


TreeEntry = tuple[bytes, bytes, bytes]


def _run_git(*arguments: str, repository_root: Path | None = None) -> bytes:
    root = Path(os.path.abspath(REPOSITORY_ROOT if repository_root is None else repository_root))
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise DeterministicEvalError("Could not resolve the evaluated source tree") from error


def _parse_head_tree(tree_listing: bytes) -> dict[bytes, TreeEntry]:
    entries: dict[bytes, TreeEntry] = {}
    try:
        for record in tree_listing.split(b"\0"):
            if not record:
                continue
            metadata, path = record.split(b"\t", maxsplit=1)
            mode, object_type, object_id = metadata.split(b" ")
            if path in entries:
                raise ValueError("duplicate path")
            entries[path] = (mode, object_type, object_id)
    except ValueError as error:
        raise DeterministicEvalError("Could not parse the committed Git tree") from error
    return entries


def _parse_index(index_listing: bytes) -> dict[bytes, tuple[bytes, bytes]]:
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    try:
        for record in index_listing.split(b"\0"):
            if not record:
                continue
            metadata, path = record.split(b"\t", maxsplit=1)
            mode, object_id, stage = metadata.split(b" ")
            if stage != b"0" or path in entries:
                raise ValueError("unmerged or duplicate index entry")
            entries[path] = (mode, object_id)
    except ValueError as error:
        raise DeterministicEvalError("Could not parse the Git index") from error
    return entries


def _reject_concealing_index_flags(flag_listing: bytes) -> None:
    for record in flag_listing.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" " or record[:1] != b"H":
            raise DeterministicEvalError(
                "Git index contains assume-unchanged, skip-worktree, or non-canonical flags"
            )


def _verify_index_matches_head(
    head_entries: dict[bytes, TreeEntry],
    index_listing: bytes,
) -> None:
    index_entries = _parse_index(index_listing)
    expected = {
        path: (mode, object_id)
        for path, (mode, _object_type, object_id) in head_entries.items()
    }
    if index_entries != expected:
        raise DeterministicEvalError("Git index does not exactly match the committed tree")


def _blob_object_id(content: bytes, expected_object_id: bytes) -> bytes:
    payload = b"blob " + str(len(content)).encode("ascii") + b"\0" + content
    if len(expected_object_id) == 40:
        return hashlib.sha1(payload, usedforsecurity=False).hexdigest().encode("ascii")
    if len(expected_object_id) == 64:
        return hashlib.sha256(payload).hexdigest().encode("ascii")
    raise DeterministicEvalError("Committed tree uses an unsupported object ID format")


def _tracked_worktree_content(
    repository_fd: int,
    relative_path: bytes,
    expected_mode: bytes,
    object_type: bytes,
) -> bytes:
    components = relative_path.split(b"/")
    if (
        not relative_path
        or relative_path.startswith(b"/")
        or any(component in {b"", b".", b".."} for component in components)
    ):
        raise DeterministicEvalError("Committed tree contains an unsafe path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent_fd = os.dup(repository_fd)
    try:
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise DeterministicEvalError(
                    "Tracked working-tree parent is not a real directory"
                )
            os.close(parent_fd)
            parent_fd = next_fd
        target_name = components[-1]
        if expected_mode == b"120000" and object_type == b"blob":
            metadata = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISLNK(metadata.st_mode):
                raise DeterministicEvalError(
                    "Tracked working-tree file type differs from the committed tree"
                )
            target = os.readlink(target_name, dir_fd=parent_fd)
            return target
        if expected_mode not in {b"100644", b"100755"} or object_type != b"blob":
            raise DeterministicEvalError("Committed tree contains an unsupported tracked type")
        descriptor = os.open(
            target_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        with os.fdopen(descriptor, "rb") as tracked_file:
            metadata = os.fstat(tracked_file.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise DeterministicEvalError(
                    "Tracked working-tree file type differs from the committed tree"
                )
            executable = bool(metadata.st_mode & stat.S_IXUSR)
            if executable is not (expected_mode == b"100755"):
                raise DeterministicEvalError(
                    "Tracked executable mode differs from the committed tree"
                )
            return tracked_file.read()
    except DeterministicEvalError:
        raise
    except OSError as error:
        raise DeterministicEvalError(
            "Could not read a tracked working-tree entry without following links"
        ) from error
    finally:
        os.close(parent_fd)


def _verify_worktree_matches_head(
    repository_root: Path,
    head_entries: dict[bytes, TreeEntry],
) -> None:
    root = Path(os.path.abspath(repository_root))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    repository_fd = -1
    try:
        path_metadata = os.lstat(root)
        if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISDIR(path_metadata.st_mode):
            raise DeterministicEvalError("Repository root must be a real directory")
        repository_fd = os.open(root, directory_flags)
        descriptor_metadata = os.fstat(repository_fd)
        if (
            descriptor_metadata.st_dev != path_metadata.st_dev
            or descriptor_metadata.st_ino != path_metadata.st_ino
        ):
            os.close(repository_fd)
            repository_fd = -1
            raise DeterministicEvalError("Repository root changed while it was opened")
    except DeterministicEvalError:
        if repository_fd >= 0:
            os.close(repository_fd)
        raise
    except OSError as error:
        if repository_fd >= 0:
            os.close(repository_fd)
        raise DeterministicEvalError("Could not open the repository root safely") from error
    try:
        for path, (mode, object_type, expected_object_id) in head_entries.items():
            content = _tracked_worktree_content(
                repository_fd,
                path,
                mode,
                object_type,
            )
            if _blob_object_id(content, expected_object_id) != expected_object_id:
                raise DeterministicEvalError(
                    "Tracked working-tree content differs from the committed tree"
                )
    finally:
        if repository_fd >= 0:
            os.close(repository_fd)


def _current_source_identity(
    repository_root: Path | None = None,
) -> tuple[str, str]:
    """Return a truthful commit/tree identity, refusing concealed divergence."""

    root = Path(os.path.abspath(REPOSITORY_ROOT if repository_root is None else repository_root))

    def run(*arguments: str) -> bytes:
        return _run_git(*arguments, repository_root=root)

    commit_sha = run("rev-parse", "HEAD").decode("ascii").strip()
    status = run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if status:
        raise DeterministicEvalError(
            "Cannot attribute a dirty source tree to commitSha; commit or clean it first"
        )
    if len(commit_sha) != 40 or any(
        character not in "0123456789abcdef" for character in commit_sha
    ):
        raise DeterministicEvalError("Source commit is not a full lowercase Git SHA")
    flags = run("ls-files", "-v", "-z")
    _reject_concealing_index_flags(flags)
    tree_listing = run("ls-tree", "-r", "-z", "--full-tree", commit_sha)
    head_entries = _parse_head_tree(tree_listing)
    _verify_index_matches_head(
        head_entries,
        run("ls-files", "--stage", "-z"),
    )
    _verify_worktree_matches_head(root, head_entries)
    final_status = run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if final_status:
        raise DeterministicEvalError(
            "Cannot attribute a dirty source tree to commitSha; commit or clean it first"
        )
    _reject_concealing_index_flags(run("ls-files", "-v", "-z"))
    if run("rev-parse", "HEAD").decode("ascii").strip() != commit_sha:
        raise DeterministicEvalError("Source commit changed during deterministic evaluation")
    return commit_sha, hashlib.sha256(tree_listing).hexdigest()


def _require_sha256(value: str, *, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise DeterministicEvalError(f"{label} is not a full lowercase SHA-256 digest")
    return value


def _validate_observation_coverage(
    *,
    eval_ids: tuple[str, ...],
    observations: ObservationSet,
) -> None:
    observed_ids = tuple(observation.eval_id for observation in observations.observations)
    if len(observed_ids) != len(eval_ids):
        raise DeterministicEvalError(
            "The staged corpus requires exactly one good observation per dataset case"
        )
    if set(observed_ids) != set(eval_ids):
        missing = sorted(set(eval_ids) - set(observed_ids))
        extra = sorted(set(observed_ids) - set(eval_ids))
        raise DeterministicEvalError(
            f"Observation coverage mismatch (missing={missing}, extra={extra})"
        )


def _validate_provenance_ground_truth(
    *,
    cases: Sequence[EvalCase],
    observations: ObservationSet,
    ground_truth: ProvenanceGroundTruthSet,
) -> None:
    """Bind independent provenance authority exactly to dataset facts and fixtures."""

    if ground_truth.dataset_version != observations.dataset_version:
        raise DeterministicEvalError(
            "Provenance ground truth and observations require the same datasetVersion"
        )
    expected_ids = tuple(case.eval_id for case in cases)
    actual_ids = tuple(item.eval_id for item in ground_truth.cases)
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != set(expected_ids):
        raise DeterministicEvalError(
            "Provenance ground truth requires exactly one case per dataset eval ID"
        )
    for case in cases:
        authority = provenance_by_id(ground_truth, case.eval_id)
        source_refs = tuple(source.source_ref for source in authority.source_catalog)
        if set(source_refs) != set(case.input.fixture_ids):
            raise DeterministicEvalError(
                f"Provenance source catalog does not match fixtures for {case.eval_id}"
            )
        expected_facts = Counter(
            (fact.field, fact.status, type(fact.value), fact.value)
            for fact in case.expectation.allowed_facts
            if fact.status.value in {"observed", "user_stated"}
        )
        authoritative_facts = Counter(
            (fact.field, fact.status, type(fact.value), fact.value)
            for fact in authority.fact_sources
        )
        if authoritative_facts != expected_facts:
            raise DeterministicEvalError(
                f"Provenance fact bindings do not match allowedFacts for {case.eval_id}"
            )


def _replace_observation(
    observations: ObservationSet,
    replacement: EvalObservation,
) -> ObservationSet:
    return observations.model_copy(
        update={
            "observations": tuple(
                replacement if item.eval_id == replacement.eval_id else item
                for item in observations.observations
            )
        }
    )


def _select_failure_sample(sample_id: str) -> FailureSample:
    samples = load_failure_samples().samples
    try:
        return next(sample for sample in samples if sample.sample_id == sample_id)
    except StopIteration as error:
        raise DeterministicEvalError(
            f"Unknown deterministic failure sample: {sample_id}"
        ) from error


def _aggregate_metrics(
    case_results: tuple[EvalCaseResult, ...],
) -> tuple[EvalMetricAggregate, ...]:
    all_checks = tuple(check for result in case_results for check in result.checks)
    aggregates: list[EvalMetricAggregate] = []
    for metric_id in EvalMetricId:
        checks = tuple(check for check in all_checks if check.metric_id is metric_id)
        denominator = len(checks)
        numerator = sum(check.passed for check in checks)
        if denominator == 0:
            status = EvalMetricStatus.NOT_APPLICABLE
            score: float | None = None
        else:
            status = (
                EvalMetricStatus.PASSED if numerator == denominator else EvalMetricStatus.FAILED
            )
            score = numerator / denominator
        aggregates.append(
            EvalMetricAggregate.model_validate(
                {
                    "contractVersion": CONTRACT_VERSION,
                    "metricId": metric_id.value,
                    "status": status.value,
                    "numerator": numerator,
                    "denominator": denominator,
                    "score": score,
                }
            )
        )
    return tuple(aggregates)


def _assert_expected_failure(
    sample: FailureSample,
    results: tuple[EvalCaseResult, ...],
) -> None:
    failures = tuple(
        (result, check)
        for result in results
        for check in result.checks
        if not check.passed
    )
    if len(failures) != 1:
        raise DeterministicEvalError(
            f"Failure sample must produce exactly one failed check, found {len(failures)}"
        )
    result, check = failures[0]
    if (
        result.eval_id != sample.base_eval_id
        or check.metric_id is not sample.expected_metric_id
        or check.observed_gate_id is not sample.expected_gate_id
        or check.observed_gate_reason_codes != sample.expected_reason_codes
    ):
        raise DeterministicEvalError(
            f"Failure sample produced an unexpected isolated failure for "
            f"{sample.expected_metric_id.value}"
        )


def build_report(
    *,
    failure_sample_id: str | None = None,
    commit_sha: str | None = None,
    source_tree_sha256: str | None = None,
) -> EvalRunSummary:
    """Build a byte-stable provider-free report for the effective staged corpus."""

    cases = load_dataset()
    observations = load_observations()
    provenance_ground_truth = load_provenance_ground_truth()
    eval_ids = tuple(case.eval_id for case in cases)
    _validate_observation_coverage(eval_ids=eval_ids, observations=observations)
    _validate_provenance_ground_truth(
        cases=cases,
        observations=observations,
        ground_truth=provenance_ground_truth,
    )

    selected_failure: FailureSample | None = None
    if failure_sample_id is not None:
        selected_failure = _select_failure_sample(failure_sample_id)
        base = observation_by_id(observations, selected_failure.base_eval_id)
        observations = _replace_observation(
            observations,
            apply_failure_sample(base, selected_failure),
        )

    case_results: list[EvalCaseResult] = []
    for case in cases:
        observation = observation_by_id(observations, case.eval_id)
        checks = grade_case(
            case,
            observation,
            provenance_by_id(provenance_ground_truth, case.eval_id),
        )
        deterministic_passed = all(check.passed for check in checks)
        result = EvalCaseResult.model_validate(
            {
                "contractVersion": CONTRACT_VERSION,
                "evalId": case.eval_id,
                "evaluationMode": EvaluationMode.DETERMINISTIC.value,
                "checks": checks,
                "providerFailure": None,
                "providerCallCount": 0,
                "deterministicPassed": deterministic_passed,
                "passed": deterministic_passed,
                "durationMs": 0,
            }
        )
        case_results.append(result)

    results = tuple(case_results)
    if selected_failure is not None:
        _assert_expected_failure(selected_failure, results)
    metrics = _aggregate_metrics(results)
    corpus_sha = _effective_corpus_sha256(
        observations=observations,
        provenance_ground_truth=provenance_ground_truth,
    )
    dataset_sha = _dataset_sha256()
    if (commit_sha is None) is not (source_tree_sha256 is None):
        raise DeterministicEvalError(
            "commitSha and source tree digest must be supplied together"
        )
    if commit_sha is None:
        resolved_commit, resolved_tree_sha = _current_source_identity()
    else:
        resolved_commit = commit_sha
        resolved_tree_sha = _require_sha256(
            source_tree_sha256 or "",
            label="Source tree digest",
        )
    if len(resolved_commit) != 40 or any(
        character not in "0123456789abcdef" for character in resolved_commit
    ):
        raise DeterministicEvalError("Source commit is not a full lowercase Git SHA")
    run_digest = hashlib.sha256(
        _canonical_bytes(
            {
                "commitSha": resolved_commit,
                "corpusSha256": corpus_sha,
                "sourceTreeSha256": resolved_tree_sha,
            }
        )
    ).hexdigest()
    passed_cases = sum(result.passed for result in results)
    deterministic_passed = all(result.deterministic_passed for result in results)
    metrics_passed = all(metric.status is not EvalMetricStatus.FAILED for metric in metrics)
    wire_time = _FIXED_EVAL_TIME.isoformat().replace("+00:00", "Z")
    summary = EvalRunSummary.model_validate(
        {
            "contractVersion": CONTRACT_VERSION,
            "runId": f"staged-deterministic-{run_digest}",
            "datasetVersion": observations.dataset_version,
            "datasetSha256": dataset_sha,
            "commitSha": resolved_commit,
            "evaluationMode": EvaluationMode.DETERMINISTIC.value,
            "startedAt": wire_time,
            "completedAt": wire_time,
            "caseResults": results,
            "metrics": metrics,
            "providerCallCount": 0,
            "totalCases": len(results),
            "passedCases": passed_cases,
            "failedCases": len(results) - passed_cases,
            "deterministicPassed": deterministic_passed,
            "passed": deterministic_passed and passed_cases == len(results) and metrics_passed,
        }
    )
    return summary


def render_report(report: EvalRunSummary) -> bytes:
    """Render canonical sorted JSON with no paths, wall clock, or random values."""

    return (
        json.dumps(
            report.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode()
        + b"\n"
    )


def write_report_atomically(
    report: EvalRunSummary,
    output_path: Path = REPORT_PATH,
    *,
    allowed_root: Path = REPOSITORY_ROOT,
) -> None:
    """Atomically replace one report below an explicit symlink-safe root."""

    root = Path(os.path.abspath(allowed_root))
    output = Path(os.path.abspath(output_path))
    try:
        relative_output = output.relative_to(root)
    except ValueError as error:
        raise DeterministicEvalError("Refusing report output outside the allowed root") from error
    if not relative_output.parts:
        raise DeterministicEvalError("Report output must name a file below the allowed root")
    try:
        root_stat = os.lstat(root)
    except OSError as error:
        raise DeterministicEvalError("Allowed report root must already exist") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise DeterministicEvalError("Allowed report root must be a real directory")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd = -1
    parent_fd = -1
    temporary_name: str | None = None
    try:
        root_fd = os.open(root, directory_flags)
        parent_fd = root_fd
        for component in relative_output.parts[:-1]:
            try:
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o755, dir_fd=parent_fd)
                next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            if parent_fd != root_fd:
                os.close(parent_fd)
            parent_fd = next_fd

        target_name = relative_output.parts[-1]
        try:
            target_stat = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            raise DeterministicEvalError("Refusing to replace a non-regular report target")

        temporary_name = f".{target_name}.{secrets.token_hex(12)}.tmp"
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(temporary_fd, "wb") as temporary:
            temporary.write(render_report(report))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary_name = None
        os.fsync(parent_fd)
    except DeterministicEvalError:
        raise
    except OSError as error:
        raise DeterministicEvalError(
            "Could not write report through the safe output path"
        ) from error
    finally:
        if temporary_name is not None and parent_fd >= 0:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent_fd)
        if parent_fd >= 0 and parent_fd != root_fd:
            os.close(parent_fd)
        if root_fd >= 0:
            os.close(root_fd)


def main(
    argv: Sequence[str] | None = None,
    *,
    output_path: Path = REPORT_PATH,
    allowed_root: Path = REPOSITORY_ROOT,
    commit_sha: str | None = None,
    source_tree_sha256: str | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--failure-sample",
        metavar="SAMPLE_ID",
        help="run one fixed negative mutation; expected to produce a non-zero result",
    )
    arguments = parser.parse_args(argv)
    try:
        report = build_report(
            failure_sample_id=arguments.failure_sample,
            commit_sha=commit_sha,
            source_tree_sha256=source_tree_sha256,
        )
        write_report_atomically(report, output_path, allowed_root=allowed_root)
    except (DeterministicEvalError, ObservationValidationError, ValueError) as error:
        print(f"Deterministic eval failed: {error}", file=sys.stderr)
        return 2
    relative_output = output_path.absolute().relative_to(allowed_root.resolve())
    print(
        "Deterministic staged eval "
        f"{'passed' if report.passed else 'failed'}: "
        f"{report.passed_cases}/{report.total_cases} cases, "
        f"provider calls={report.provider_call_count}, report={relative_output}"
    )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
