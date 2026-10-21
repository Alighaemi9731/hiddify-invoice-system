"""Shared, validated broadcast segmentation for the web panel and owner bot."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveId = Annotated[int, Field(gt=0)]
Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class BroadcastFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    panel_ids: list[PositiveId] = Field(default_factory=list, max_length=50)
    period_from: Month | None = None
    period_to: Month | None = None
    access_state: Literal["active", "frozen", "enforced"] | None = None
    min_debt: float | None = Field(default=None, ge=0, le=1e15)
    max_debt: float | None = Field(default=None, ge=0, le=1e15)
    min_age_days: int | None = Field(default=None, ge=0, le=36500)
    max_age_days: int | None = Field(default=None, ge=0, le=36500)
    exclude_pending: bool = False
    search: str = Field(default="", max_length=100)
    excluded_reseller_ids: list[PositiveId] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def ordered_ranges(self) -> Self:
        ranges: list[tuple[Any, Any]] = [(self.period_from, self.period_to), (self.min_debt, self.max_debt),
                                        (self.min_age_days, self.max_age_days)]
        for low, high in ranges:
            if low is not None and high is not None and low > high:
                raise ValueError("ابتدای بازه باید کوچک‌تر یا مساوی انتهای آن باشد.")
        return self
