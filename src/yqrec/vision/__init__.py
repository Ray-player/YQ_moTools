"""视觉子包：键位检测、采样、阈值标定。"""

from .keygrid import (
    circle_map_note,
    correct_button_coordinates,
    detect_keys,
    keygrid_from_circles,
    keygrid_from_note_map,
    keygrid_from_points,
)
from .sampler import KeySampler
from .thresholds import CalibrationResult, calibrate

__all__ = [
    "CalibrationResult",
    "KeySampler",
    "calibrate",
    "circle_map_note",
    "correct_button_coordinates",
    "detect_keys",
    "keygrid_from_circles",
    "keygrid_from_note_map",
    "keygrid_from_points",
]
