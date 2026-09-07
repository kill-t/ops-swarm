"""取证材料与任务范围、版本和散列不能互相替代。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.tasks.evidence import (
    TaskEvidenceRecord,
    TaskEvidenceSnapshot,
    evidence_hash,
)
from tests.task_evidence_helpers import snapshot
from tests.task_facts_helpers import BASE_TIME


@pytest.mark.parametrize("change", ["duplicate", "role", "time", "source", "size"])
def test_snapshot_rejects_invalid_evidence(change):
    data = snapshot().model_dump(mode="json")
    if change == "duplicate":
        data["evidence"] *= 2
    elif change == "role":
        data["evidence"][0]["kind"] = "metric"
    elif change == "time":
        data["evidence"][0]["observed_at"] = BASE_TIME.isoformat()
    elif change == "source":
        data["evidence"][0]["source"] = " "
    else:
        data["evidence"][0]["attributes"] = {"long_log": "x" * 131072}
    with pytest.raises(ValidationError):
        TaskEvidenceSnapshot.model_validate(data)


def test_empty_snapshot_is_a_fact_and_hash_covers_scope():
    original = snapshot()
    data = original.model_dump(mode="json")
    data["evidence"] = []
    empty = TaskEvidenceSnapshot.model_validate(data)
    assert empty.evidence == () and evidence_hash(empty) != evidence_hash(original)
    shifted = empty.model_copy(update={"window_start": empty.window_start - timedelta(minutes=1)})
    assert evidence_hash(shifted) != evidence_hash(empty)
    record = dict(
        task_id=uuid4(),
        investigation_run_id=uuid4(),
        attempt_id=uuid4(),
        call_id=uuid4(),
        snapshot=empty,
        content_sha256=evidence_hash(empty),
        created_at=BASE_TIME,
    )
    assert TaskEvidenceRecord(**record).snapshot == empty
    with pytest.raises(ValidationError, match="散列"):
        TaskEvidenceRecord(**{**record, "snapshot": shifted})
