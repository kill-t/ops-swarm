"""变更记录 PostgreSQL 持久化。"""

from opsswarm.change_records.persistence.repository import ChangeRecordStore
from opsswarm.change_records.persistence.tables import ChangeRecordRow

__all__ = ["ChangeRecordRow", "ChangeRecordStore"]
