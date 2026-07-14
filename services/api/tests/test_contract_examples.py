import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from claimdone_api.contracts import ClaimPacket

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = REPOSITORY_ROOT / "contracts" / "examples"


def load_example(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((EXAMPLES / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", ["happy_path.json", "block.json", "mismatch.json"])
def test_committed_examples_validate_from_json(name: str) -> None:
    source = (EXAMPLES / name).read_text(encoding="utf-8")

    packet = ClaimPacket.model_validate_json(source)

    assert packet.contract_version == "1.0.0"
    assert packet.scope.agent_can_submit is False
    assert packet.plan.agent_can_submit is False


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        ("happy_path.json", "unknown_root_field"),
        ("happy_path.json", "missing_provenance"),
        ("block.json", "gate_override"),
        ("mismatch.json", "verification_override"),
        ("happy_path.json", "incomplete_verification"),
        ("happy_path.json", "unknown_enum"),
    ],
)
def test_invalid_example_variants_are_rejected(name: str, mutation: str) -> None:
    data = deepcopy(load_example(name))
    if mutation == "unknown_root_field":
        data["unrecognized"] = True
    elif mutation == "missing_provenance":
        data["facts"][0]["sourceRefs"] = ["prov-does-not-exist"]
    elif mutation == "gate_override":
        data["gateDecisions"][0]["passed"] = True
    elif mutation == "verification_override":
        data["verification"]["reviewAllowed"] = True
    elif mutation == "incomplete_verification":
        data["verification"]["fieldResults"].pop()
    elif mutation == "unknown_enum":
        data["facts"][0]["status"] = "guessed"
    else:  # pragma: no cover - the parameter table is exhaustive
        raise AssertionError(f"Unhandled mutation: {mutation}")

    with pytest.raises(ValidationError):
        ClaimPacket.model_validate_json(json.dumps(data))
