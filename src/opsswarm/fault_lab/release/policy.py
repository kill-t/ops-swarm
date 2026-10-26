"""不依赖HTTP、数据库或框架的版本比较与修订规则。"""

import hashlib
import json

from opsswarm.fault_lab.release.contracts import (
    LegacyFaultCommand,
    ReleaseCommand,
    ReleaseError,
    ReleaseVersion,
)


def command_hash(command: object) -> str:
    return hashlib.sha256(
        json.dumps(
            command, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def transition(
    version: ReleaseVersion,
    revision: int,
    command: ReleaseCommand | LegacyFaultCommand,
) -> tuple[ReleaseVersion, int]:
    if type(revision) is not int or not 1 <= revision <= 2147483647:
        raise ValueError("修订必须是正整数")
    if version not in {"stable", "2026.01.01-rc1"}:
        raise ValueError("演练版本不在固定目录")
    if isinstance(command, ReleaseCommand):
        if command.expected_version != version or command.expected_revision != revision:
            raise ReleaseError("lab_release_cas_conflict", "演练版本或修订已变化")
        target = command.target_version
    else:
        target = "2026.01.01-rc1" if command.enabled else "stable"
    result = revision + (target != version)
    if result > 2147483647:
        raise ReleaseError("lab_release_revision_exhausted", "演练修订已达上限")
    return target, result
