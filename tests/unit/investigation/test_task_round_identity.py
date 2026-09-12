"""轮次散列在持久化重构后保持历史兼容。"""

from opsswarm.investigation.tasks.identity import round_content_hash
from tests.task_facts_helpers import assignment


def test_chinese_assignment_keeps_legacy_round_hash():
    assert (
        round_content_hash(assignment(count=3))
        == "0df3286a4fbe4672e19bedbb1ecfca7dd82bd20faba420a50deccd17000a7c5e"
    )
