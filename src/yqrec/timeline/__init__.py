"""时间线子包：判按状态机与事件装配。"""

from .assembler import AssembleConfig, assemble
from .detector import DetectorConfig, PressDetector

__all__ = ["AssembleConfig", "DetectorConfig", "PressDetector", "assemble"]
