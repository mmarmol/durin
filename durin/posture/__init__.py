"""Durin posture vector — persistent behavioral bias system."""

from durin.posture.goal_bias import compute_goal_bias
from durin.posture.hook import PostureHook
from durin.posture.homeostasis import update_axis, update_vector
from durin.posture.phrase import generate_posture_phrase
from durin.posture.stimulus import StimulusEvent, StimulusTable
from durin.posture.vector import AxisName, AxisState, PostureVector

__all__ = [
    "AxisName",
    "AxisState",
    "PostureHook",
    "PostureVector",
    "StimulusEvent",
    "StimulusTable",
    "compute_goal_bias",
    "generate_posture_phrase",
    "update_axis",
    "update_vector",
]
