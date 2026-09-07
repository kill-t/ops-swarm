"""冻结场景的版本化内容身份，不依赖集合或字典的迭代顺序。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from opsswarm.investigation.frozen_legacy_ids import LEGACY_SNAPSHOT_IDS

if TYPE_CHECKING:
    from opsswarm.investigation.scenarios import InvestigationScenario


def frozen_snapshot_id(scenario: InvestigationScenario) -> str:
    data = scenario.model_dump(mode="json")
    data["required_evidence_ids"] = sorted(scenario.required_evidence_ids)
    payload = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "frozen-v2:" + hashlib.sha256(payload).hexdigest()


def legacy_snapshot_ids(snapshot_id: str) -> frozenset[str]:
    """只有完整内容身份匹配已发布版本，才授权沿用其已知旧身份。"""

    return LEGACY_SNAPSHOT_IDS.get(snapshot_id, frozenset())
