"""冻结内容身份跨进程稳定，旧别名只能授权完全匹配的发布内容。"""

import hashlib
import json
import os
import re
import subprocess
import sys
from itertools import permutations
from pathlib import Path

import pytest

from opsswarm.investigation import scenarios
from opsswarm.investigation.frozen_identity import frozen_snapshot_id, legacy_snapshot_ids

PUBLISHED = tuple(
    value
    for value in vars(scenarios).values()
    if isinstance(value, scenarios.InvestigationScenario)
)
ROOT = Path(__file__).resolve().parents[3]


def test_independent_process_hash_seeds_have_identical_identity():
    code = (
        "from opsswarm.investigation.scenarios import FrozenScenarioDataSource, "
        "RELEASE_500_SCENARIO; "
        "print(FrozenScenarioDataSource(RELEASE_500_SCENARIO).snapshot_id)"
    )
    values = [
        subprocess.check_output(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            timeout=30,
        ).strip()
        for seed in ("1", "2", "3")
    ]
    assert len(set(values)) == 1 and values[0].startswith("frozen-v2:")
    assert len(values[0]) <= 100


@pytest.mark.parametrize("scenario", PUBLISHED, ids=lambda s: s.scenario_id)
def test_all_published_legacy_permutations_are_registered(scenario):
    assert len(scenario.required_evidence_ids) <= 3
    raw = scenario.model_dump_json()
    legacy = set()
    for order in permutations(sorted(scenario.required_evidence_ids)):
        old_json = re.sub(
            r'"required_evidence_ids":\[[^]]*\]',
            '"required_evidence_ids":' + json.dumps(order, separators=(",", ":")),
            raw,
        )
        legacy.add("frozen:" + hashlib.sha256(old_json.encode()).hexdigest())
    current = frozen_snapshot_id(scenario)
    assert legacy_snapshot_ids(current) == legacy
    assert scenarios.FrozenScenarioDataSource(scenario).legacy_snapshot_ids == legacy


def test_dictionary_and_set_order_do_not_change_new_identity():
    original = scenarios.RELEASE_500_SCENARIO
    data = original.model_dump(mode="json")
    data["required_evidence_ids"] = list(reversed(data["required_evidence_ids"]))
    for evidence in data["evidence"]:
        evidence["attributes"] = dict(reversed(list(evidence["attributes"].items())))
    reordered = scenarios.InvestigationScenario.model_validate(dict(reversed(list(data.items()))))
    assert frozen_snapshot_id(original) == frozen_snapshot_id(reordered)


@pytest.mark.parametrize("field", ["scenario_id", "source", "summary", "time", "truth", "required"])
def test_changed_content_does_not_inherit_legacy_identity(field):
    original = scenarios.RELEASE_500_SCENARIO
    data = original.model_dump(mode="json")
    if field == "scenario_id":
        data["scenario_id"] = "custom-scenario"
    elif field in ("source", "summary"):
        data["evidence"][0][field] = "changed-material"
    elif field == "time":
        data["evidence"][0]["observed_at"] = "2026-01-01T08:07:00Z"
    elif field == "truth":
        data["expected_recommendation"] = "continue_investigation"
    else:
        data["required_evidence_ids"] = data["required_evidence_ids"][:1]
    changed = scenarios.InvestigationScenario.model_validate(data)
    current = frozen_snapshot_id(changed)
    assert current != frozen_snapshot_id(original)
    assert legacy_snapshot_ids(current) == frozenset()


def test_unknown_legacy_identity_is_not_authorized_by_prefix():
    assert legacy_snapshot_ids("frozen:" + "0" * 64) == frozenset()
