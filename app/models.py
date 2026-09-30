"""HTTP 请求/响应模型与校验。

时间输入必须带时区，服务端统一转为 UTC。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator
from typing_extensions import Annotated

# UTC 时间定长格式（6 位微秒 + Z），保证字典序==时间序
UTC_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def to_utc_string(dt: datetime) -> str:
    """把带时区的 datetime 归一化为 UTC 定长字符串。"""
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime(UTC_FMT)


def _require_aware(dt: datetime) -> datetime:
    """拒绝无时区时间（naive datetime），统一按 422 处理。"""
    if dt.tzinfo is None:
        raise ValueError("datetime must include a timezone, e.g. 2026-01-01T00:00:00Z")
    return dt


UtcDatetime = Annotated[
    datetime,
    AfterValidator(_require_aware),
    Field(description="带时区的 UTC 时间，例如 2026-01-01T00:00:00Z"),
]


class CertPayload(BaseModel):
    """证书内容：左闭右开有效期 [valid_from, valid_to)，闭区间允许值 [min, max]。"""

    certificate_no: str = Field(min_length=1, max_length=128)
    device_no: str = Field(min_length=1, max_length=128)
    valid_from: UtcDatetime
    valid_to: UtcDatetime
    min_value: float
    max_value: float

    @field_validator("min_value", "max_value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("value must be finite")
        return v

    @model_validator(mode="after")
    def _check_ranges(self) -> "CertPayload":
        if self.valid_from >= self.valid_to:
            raise ValueError("valid_from must be earlier than valid_to (half-open interval)")
        if self.min_value > self.max_value:
            raise ValueError("min_value must not be greater than max_value (closed range)")
        return self


class CertIssueRequest(CertPayload):
    request_id: str = Field(min_length=1, max_length=128)


class CertCorrectRequest(BaseModel):
    """纠正：为已存在的证书编号登记新版本，旧版本在新版本起失效。"""

    request_id: str = Field(min_length=1, max_length=128)
    certificate_no: str = Field(min_length=1, max_length=128)
    device_no: str = Field(min_length=1, max_length=128)
    valid_from: UtcDatetime
    valid_to: UtcDatetime
    min_value: float
    max_value: float
    reason: Optional[str] = Field(default=None, max_length=512)

    @field_validator("min_value", "max_value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("value must be finite")
        return v

    @model_validator(mode="after")
    def _check_ranges(self) -> "CertCorrectRequest":
        if self.valid_from >= self.valid_to:
            raise ValueError("valid_from must be earlier than valid_to (half-open interval)")
        if self.min_value > self.max_value:
            raise ValueError("min_value must not be greater than max_value (closed range)")
        return self


class CertRevokeRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    certificate_no: str = Field(min_length=1, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=512)


class ReadingItem(BaseModel):
    reading_no: str = Field(min_length=1, max_length=128)
    device_no: str = Field(min_length=1, max_length=128)
    measured_at: UtcDatetime
    value: float

    @field_validator("value")
    @classmethod
    def _finite(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("value must be finite")
        return v


class ReadingSubmitRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    readings: List[ReadingItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_reading_no(self) -> "ReadingSubmitRequest":
        nos = [r.reading_no for r in self.readings]
        if len(set(nos)) != len(nos):
            raise ValueError("duplicate reading_no within the same request")
        return self
