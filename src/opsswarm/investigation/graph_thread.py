"""调查运行与 LangGraph thread 的稳定标识规则。"""

import re
from uuid import UUID

GRAPH_THREAD_ID_PREFIX = "investigation-run"
_GRAPH_THREAD_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,99}$")


def build_graph_thread_id(run_id: UUID) -> str:
    """使用业务运行 ID 生成确定且可审计的 LangGraph thread_id。"""

    return f"{GRAPH_THREAD_ID_PREFIX}:{run_id}"


def normalize_graph_thread_id(value: str) -> str:
    """拒绝空白、超长或不适合作为持久标识的 thread_id。"""

    normalized = value.strip()
    if not _GRAPH_THREAD_ID_PATTERN.fullmatch(normalized):
        raise ValueError("graph_thread_id 必须是 1 到 100 位安全标识字符")
    return normalized
