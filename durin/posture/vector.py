"""Posture vector data model — 5-axis behavioral bias state."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class AxisName(StrEnum):
    CAUTELA = "cautela"
    EXPLORACION = "exploracion"
    PROFUNDIDAD = "profundidad"
    DISCIPLINA = "disciplina"
    CONFORMIDAD = "conformidad"


class AxisState(BaseModel, frozen=True):
    media: float = Field(ge=0.0, le=1.0)
    varianza: float = Field(gt=0.0, le=0.5)
    fuerza_retorno: float = Field(ge=0.0, le=1.0)
    valor_actual: float = Field(ge=0.0, le=1.0)


class PostureVector(BaseModel, frozen=True):
    axes: dict[AxisName, AxisState]

    @classmethod
    def default(cls) -> PostureVector:
        return cls(axes={
            AxisName.CAUTELA: AxisState(
                media=0.6, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.6,
            ),
            AxisName.EXPLORACION: AxisState(
                media=0.4, varianza=0.20, fuerza_retorno=0.4, valor_actual=0.4,
            ),
            AxisName.PROFUNDIDAD: AxisState(
                media=0.5, varianza=0.20, fuerza_retorno=0.5, valor_actual=0.5,
            ),
            AxisName.DISCIPLINA: AxisState(
                media=0.5, varianza=0.15, fuerza_retorno=0.2, valor_actual=0.5,
            ),
            AxisName.CONFORMIDAD: AxisState(
                media=0.7, varianza=0.15, fuerza_retorno=0.3, valor_actual=0.7,
            ),
        })

    def snapshot(self) -> dict[AxisName, float]:
        return {name: state.valor_actual for name, state in self.axes.items()}

    def with_update(self, updates: dict[AxisName, AxisState]) -> PostureVector:
        new_axes = {**self.axes, **updates}
        return PostureVector(axes=new_axes)
