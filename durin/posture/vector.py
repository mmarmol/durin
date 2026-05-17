"""Posture vector data model — 5-axis behavioral bias state."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class AxisName(StrEnum):
    CAUTION = "caution"
    EXPLORATION = "exploration"
    DEPTH = "depth"
    DISCIPLINE = "discipline"
    CONFORMITY = "conformity"


class AxisState(BaseModel, frozen=True):
    mean: float = Field(ge=0.0, le=1.0)
    variance: float = Field(gt=0.0, le=0.5)
    return_force: float = Field(ge=0.0, le=1.0)
    current_value: float = Field(ge=0.0, le=1.0)


class PostureVector(BaseModel, frozen=True):
    axes: dict[AxisName, AxisState]

    @classmethod
    def default(cls) -> PostureVector:
        return cls(axes={
            AxisName.CAUTION: AxisState(
                mean=0.6, variance=0.15, return_force=0.3, current_value=0.6,
            ),
            AxisName.EXPLORATION: AxisState(
                mean=0.4, variance=0.20, return_force=0.4, current_value=0.4,
            ),
            AxisName.DEPTH: AxisState(
                mean=0.5, variance=0.20, return_force=0.5, current_value=0.5,
            ),
            AxisName.DISCIPLINE: AxisState(
                mean=0.5, variance=0.15, return_force=0.2, current_value=0.5,
            ),
            AxisName.CONFORMITY: AxisState(
                mean=0.7, variance=0.15, return_force=0.3, current_value=0.7,
            ),
        })

    def snapshot(self) -> dict[AxisName, float]:
        return {name: state.current_value for name, state in self.axes.items()}

    def with_update(self, updates: dict[AxisName, AxisState]) -> PostureVector:
        new_axes = {**self.axes, **updates}
        return PostureVector(axes=new_axes)
