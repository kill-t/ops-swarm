"""各只读来源共用的 UTC 观测范围，不承担取证或业务状态。"""

from datetime import UTC, datetime, timedelta
from typing import Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator, model_validator


class ObservationWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_start: AwareDatetime
    window_end: AwareDatetime

    @field_validator("window_start", "window_end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        value = value.astimezone(UTC)
        if value.microsecond or value < datetime(1970, 1, 1, tzinfo=UTC):
            raise ValueError("观测窗口必须使用 Unix 起点之后的整秒时间")
        return value

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        if not timedelta(0) < self.window_end - self.window_start <= timedelta(hours=24):
            raise ValueError("观测窗口必须为正且不超过 24 小时")
        return self

    def require_elapsed(self, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None or self.window_end > now:
            raise ValueError("观测截止时间不能晚于带时区的当前时间")

    def telemetry_minutes(self) -> int:
        seconds = int((self.window_end - self.window_start).total_seconds())
        if seconds % 60 or not 60 <= seconds <= 3600:
            raise ValueError("遥测窗口必须为 1 到 60 个完整分钟")
        return seconds // 60
