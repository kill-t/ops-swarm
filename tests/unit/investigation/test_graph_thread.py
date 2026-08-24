from uuid import UUID

import pytest

from opsswarm.investigation.graph_thread import (
    build_graph_thread_id,
    normalize_graph_thread_id,
)


def test_graph_thread_id_is_deterministic_and_safe() -> None:
    run_id = UUID("00000000-0000-0000-0000-000000000123")

    thread_id = build_graph_thread_id(run_id)

    assert thread_id == "investigation-run:00000000-0000-0000-0000-000000000123"
    assert normalize_graph_thread_id(f"  {thread_id}  ") == thread_id


@pytest.mark.parametrize(
    "value",
    ["", "contains space", "../unsafe", "x" * 101],
)
def test_graph_thread_id_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError, match="安全标识字符"):
        normalize_graph_thread_id(value)
