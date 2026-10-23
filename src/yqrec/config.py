"""应用配置与配置档（01 §8.4 / 03 §5.2）。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging import get_logger
from .types import CaptureTarget, KeyGrid, KeyThreshold, Profile, target_from_dict

CONFIG_VERSION = 4
"""配置版本。

- 1 → 2（M2q.4）：`record.min_hold_ms` 默认值 25 → 0（单帧即确认）。
  旧文件里的 25 是当时的固定值、不是用户选择，继续沿用会让"只占 1 帧"的轻按被丢掉，
  所以 `load_config()` 对 version<2 的档强制重置（见 `_migrate`）。
- 2 → 3（M3a）：`ui.theme` 重置为 `light`（产品默认日间）。M3a 之前**没有主题切换 UI**，
  旧文件里的 `"dark"` 只能是"当时的默认值被 `asdict(ui)` 序列化"，不是用户选的主题；
  直接沿用会让升级后首次启动变成夜间（用户实测反馈过这一条）。
- 3 → 4（M3e）：`backend` 重置为 `hdr`（j-HDR 成为默认截图方式）。此前 UI **从不回写**
  `AppConfig.backend`，档里出现的 `"dxgi"` 只是 `to_dict()` 序列化出的默认值，不是用户选择；
  不重置的话"改默认"对已有档完全无效（它们会被读回 dxgi）。从 v4 起这个字段才代表真实选择。
"""

# 首次启动（没有 `settings.json`）时的窗口状态。取用户实机调好的值，
# 这样"清掉 C 盘缓存 / 换机首次运行"也能直接落在工作位置。
DEFAULT_WINDOW_WIDTH = 558
DEFAULT_WINDOW_HEIGHT = 913
"""窗口客户区尺寸（窄长条竖排，取用户实机调好的高度 + 当前布局能落地的最窄宽度）。

注：`UiConfig.window_width` 更早记的是 520，但 M2q 加了「半径」滑条后布局最小宽涨到 554，
520 会被 `_clamp_window_size()` 抬到 554~558 —— 所以默认值直接写实际值，避免"默认和显示不一致"。
"""

DEFAULT_MIN_HOLD_MS = 0.0
"""按下确认所需的最短持续时长（0 = 单帧即确认）。

必须用**模块常量**而不是 `RecordConfig.min_hold_ms` 取默认值：`slots=True` 的 dataclass
把字段变成 `__slots__` 里的 member descriptor，类属性访问拿到的是 descriptor 对象本身，
写进配置会变成不可序列化的脏值（M2q.7 踩过）。
"""

DEFAULT_WINDOW_X = 1393
DEFAULT_WINDOW_Y = 50
"""窗口左上角坐标（主屏 1920×1032 可用区的右下贴边）。

只是"默认建议位置"：`_restore_window_position()` 会校验它是否与某块屏相交，
不在屏内（换显示器 / 拔屏 / 只留副屏）就拉回主屏居中，不会被丢到屏幕外。
"""


@dataclass(slots=True)
class RecordConfig:
    hotkey: str = "F9"
    chord_window_ms: float = 25.0
    min_hold_ms: float = DEFAULT_MIN_HOLD_MS
    """按下确认需要持续多久（0 = 单帧即确认，M2q.4 默认）。

    `> 0` 会把"只占 1 帧"的轻按整场丢掉（低帧率/回退 GDI 时尤其明显）；
    真遇到特效造成的单帧误判，调回 25~40 即可。
    """
    min_gap_ms: float = 30.0
    debug_ringbuffer: bool = False


@dataclass(slots=True)
class AudioConfig:
    enabled: bool = True
    volume: float = 0.8
    offset_ms: int = 0


@dataclass(slots=True)
class UiConfig:
    language: str = "zh-CN"
    theme: str = "light"
    """界面主题：`light`（默认，日间）/ `dark`（夜间）。首次运行走日间。"""
    hud: bool = True
    always_on_top: bool = True
    """窗口置顶（单显示器贴游戏用）；默认开，退出时记住状态。"""
    window_width: int = DEFAULT_WINDOW_WIDTH
    window_height: int = DEFAULT_WINDOW_HEIGHT
    """上次退出时窗口的客户区尺寸（缩放后记住，下次启动沿用；非法值会被夹到合理范围）。"""
    window_maximized: bool = False
    """上次退出时是否最大化；为真时启动直接最大化（不覆盖 `window_width/height`）。"""
    window_x: int = DEFAULT_WINDOW_X
    window_y: int = DEFAULT_WINDOW_Y
    """上次退出时窗口左上角坐标（默认值即首次运行的建议位置）。

    两个都为负（-1/-1 = 未记录）表示"不定位，由系统决定"；否则启动时校验是否还在某块屏内。
    """
    sample_mode: str = "ring"
    """检测色值方式：`ring`（只采外环，默认）/ `square`（整块 2R 方形，含中心图标）。"""
    square_offset_y: int = 0
    """`square` 模式下采样区域相对检测圈中心的纵向偏移（1920 宽基准的像素，正数向下）。"""
    instrument: str = ""
    """上次选中的乐器 id（空 = 没选过 / 老档）。

    必须存在这里、而不是只靠标定档：标定档（`profiles/default.json`）只在**重新标定**时
    才写，换了乐器却没重新标定的话，启动时会被标定档里的旧乐器一把拽回去
    （2026-10-23 用户实机反馈："乐器一直固定在老旧的诗琴"）。空值保留"跟随标定档"的
    旧行为，老档升级后不会丢标定。
    """


@dataclass(slots=True)
class PathConfig:
    last_score_dir: str = ""
    """上次保存/读取谱面的目录（空 = 用默认 `<文档>\\YQ_moTools`）。

    只记"文件夹"，不记文件名 —— 下次打开对话框直接落到同一处。
    """


@dataclass(slots=True)
class AppConfig:
    version: int = CONFIG_VERSION
    target: CaptureTarget | None = None
    backend: str = "hdr"  # j-HDR（HDR 屏正解）；可选 "dxgi" / "gdi"
    profile: str = ""
    record: RecordConfig = field(default_factory=RecordConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    paths: PathConfig = field(default_factory=PathConfig)

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        from .types import _target_to_dict  # 内部辅助

        return {
            "version": self.version,
            "backend": self.backend,
            "profile": self.profile,
            "target": _target_to_dict(self.target) if self.target else None,
            # 子配置都是 slots dataclass（没有 __dict__），必须走 asdict
            "record": asdict(self.record),
            "audio": asdict(self.audio),
            "ui": asdict(self.ui),
            "paths": asdict(self.paths),
        }


DATA_DIR_NAME = "YQ_moTools"
r"""数据目录名（`%APPDATA%\YQ_moTools`、`<文档>\YQ_moTools`）。

跟产品名一致（M3b.1）。旧名 `YQRec` 的目录**不做迁移**：里面是标定与窗口状态，
用户明确表示"重新标定一次就行"，所以 `M3b.1` 直接删掉了旧目录。
"""

ARCHIVE_LOCAL = "local"
"""`archive_path` 的 `Local`：存档放 `<exe目录>\\data`（便携、不碰 C 盘）。"""

ARCHIVE_APPDATA = "temp"
"""`archive_path` 的 `Temp`：存档放 `%APPDATA%\\YQ_moTools`（写不进来自动降级 Local）。"""

_APPDATA_ALIASES = (ARCHIVE_APPDATA, "appdata", "default", "roaming")
"""`Temp` 这一档的别名（用户手写 `AppData` / 默认档也认）。"""

_log = get_logger("config")


def _env_dir(name: str) -> Path | None:
    """读一个环境变量当目录用；**非绝对路径一律不认**（返回 None）。

    这一点是硬约束：`APPDATA` 为空/被写成相对路径时，`Path("")/"YQ_moTools"/"logs"`
    会落到**当前工作目录**（= exe 所在目录），于是日志、设置、标定全被写到运行目录，
    而用户以为它们还在 `%APPDATA%`（2026-10-23 实机：运行目录凭空多出 logs/，
    `theme` 的 `%TEMP%` 缓存也退化成 cwd）。宁可报错，也不往 cwd 写。
    """
    raw = os.environ.get(name)
    if not raw:
        return None
    text = os.path.expandvars(raw.strip().strip('"'))
    if not text:
        return None
    path = Path(text)
    return path if path.is_absolute() else None


def _home_dir() -> Path:
    """用户主目录（保证绝对路径；拿不到就抛错，不返回相对路径）。"""
    home = _env_dir("USERPROFILE") or _env_dir("HOME")
    if home is not None:
        return home
    try:
        candidate = Path.home()
    except Exception:  # pragma: no cover - 极端环境
        candidate = None
    if candidate is not None and candidate.is_absolute():
        return candidate
    raise RuntimeError("无法确定用户主目录（APPDATA / USERPROFILE / HOME 均不可用）")


def _roaming_from_registry() -> Path | None:
    """注册表兜底：`HKCU\\...\\User Shell Folders\\AppData`（环境变量被剥掉时仍有值）。"""
    try:
        import winreg  # noqa: PLC0415 - 仅 Windows 需要
    except ImportError:  # pragma: no cover - 非 Windows
        return None
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            raw, _kind = winreg.QueryValueEx(key, "AppData")
    except OSError:
        return None
    if not isinstance(raw, str):
        return None
    path = Path(os.path.expandvars(raw))
    return path if path.is_absolute() else None


def probe_writable(path: Path | str) -> str | None:
    """试写一个探测文件，返回 None 表示可写，否则返回错误说明。

    光看 ACL 不够：实测（2026-10-23）`%APPDATA%\\YQ_moTools` 三个文件的 ACL 全是
    `Administrator:(F)`，PowerShell 能写，但某些进程 `open(..., 'a')` 仍报
    `PermissionError: [Errno 13]` —— 只有"真写一次"才靠得住。
    """
    import uuid

    target_dir = Path(path)
    probe = target_dir / f".write_probe_{uuid.uuid4().hex[:8]}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def data_dir() -> Path:
    r"""数据目录（**绝对路径**）：设置 / 标定 / 日志都在它下面。

    解析顺序：
    1. `YQREC_DATA_DIR`（显式覆盖：测试、便携版、自动降级都用它）；
    2. `assets/instruments.json` 的 **`archive_path`**（用户可配置）：
       `"Local"` → `<exe目录>\data`；`"Temp"` → `%APPDATA%\YQ_moTools`；其他 = 绝对路径；
    3. 默认 `%APPDATA%\YQ_moTools`，其中 `%APPDATA%` 拿不到就退注册表、再退主目录。

    任何一步拿到相对路径都视为无效继续往下找，最终仍失败就抛 `RuntimeError` ——
    绝不让数据落到 cwd（否则日志/设置会写进 exe 所在目录）。
    """
    override = _env_dir("YQREC_DATA_DIR")
    if override is not None:
        return override
    custom = archive_path_dir()
    if custom is not None:
        return custom
    return _roaming_dir() / DATA_DIR_NAME


def exe_dir() -> Path:
    """程序所在目录：打包后是 exe 目录，源码运行是项目根（`Local` 档的基准）。"""
    import sys

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def local_data_dir() -> Path:
    """`archive_path = "Local"` 的目标：`<exe目录>\\data`（便携、不碰 C 盘）。"""
    return exe_dir() / "data"


def _roaming_dir() -> Path:
    """`%APPDATA%`（拿不到就退注册表、再退 `<主目录>\\AppData\\Roaming`）。"""
    roaming = _env_dir("APPDATA") or _roaming_from_registry()
    if roaming is None:
        roaming = _home_dir() / "AppData" / "Roaming"
    return roaming


def archive_setting() -> str:
    """`assets/instruments.json` 的 `archive_path` 原值（读不到返回空串）。"""
    try:
        from .instruments import load_archive_path  # 延迟导入：instruments 依赖 types，避免顶层环

        return load_archive_path()
    except Exception as exc:  # pragma: no cover - 极端情况（文件被删/JSON 坏）
        _log.warning("读取 archive_path 失败（%s），按默认存档位置处理", exc)
        return ""


def archive_path_dir(setting: str | None = None) -> Path | None:
    """把 `archive_path` 解析成目录；未配置 / 非法返回 None（= 走默认 %APPDATA%）。"""
    raw = archive_setting() if setting is None else setting
    raw = (raw or "").strip().strip('"')
    if not raw:
        return None
    text = os.path.expandvars(os.path.expanduser(raw))
    key = text.strip().lower()
    if key == ARCHIVE_LOCAL:
        return local_data_dir()
    if key in _APPDATA_ALIASES:
        return _roaming_dir() / DATA_DIR_NAME
    path = Path(text)
    if path.is_absolute():
        return path
    _log.warning("archive_path=%r 既不是 Local/Temp 也不是绝对路径，按默认位置处理", raw)
    return None


def config_dir() -> Path:
    return data_dir()


def config_path() -> Path:
    return config_dir() / "settings.json"


def default_config() -> AppConfig:
    return AppConfig()


def _migrate(data: dict[str, Any]) -> dict[str, Any]:
    """把旧版本的 `settings.json` 就地升级到当前语义（原地改 dict 并返回）。

    只做"旧值已失去意义"的迁移，不碰用户主动调过的字段：
    - v1 → v2：`record.min_hold_ms` 重置为 0。旧版把它固定写死 25ms，语义是
      "至少两帧可见才确认"；M2q.4 改成"单帧即确认"后，文件里残留的 25 会让修复失效
      （实测：轻按仍被吃掉），所以按版本号强制对齐。
    - v2 → v3：`ui.theme` 重置为 `light`。M3a 才引入"日间/夜间"切换与持久化，
      旧档里的 `"dark"` 来自**旧默认值被序列化**（当时界面上根本没有切换入口），
      不是用户选择；不重置的话升级后首次启动会直接进夜间（用户实机反馈）。
      用户在 M3a 之后主动切到夜间 → 档里是 version 3，不会再被这条迁移覆盖。
    """
    version = int(data.get("version") or 1)
    if version < 2:
        record = data.get("record")
        if isinstance(record, dict):
            record["min_hold_ms"] = DEFAULT_MIN_HOLD_MS
        data["version"] = 2
    if version < 3:
        ui = data.get("ui")
        if isinstance(ui, dict):
            ui["theme"] = "light"
        data["version"] = CONFIG_VERSION  # 迁移只改内存；下次 _save_app_config() 落盘时写回新版本
        # （迁移是幂等的，重复跑无害 —— 用户不改任何设置时文件可能一直是旧 version）
    if version < 4:
        # M3e：默认截图方式改 j-HDR。v4 之前 UI 从不回写 backend，
        # 档里的值（通常是 "dxgi"）是序列化的默认值，不是用户选择 → 按版本号强制对齐。
        data["backend"] = "hdr"
        data["version"] = CONFIG_VERSION
    return data


def load_config(path: Path | str | None = None) -> AppConfig:
    p = Path(path) if path else config_path()
    if not p.exists():
        return default_config()
    data = _migrate(json.loads(p.read_text(encoding="utf-8")))
    cfg = default_config()
    cfg.version = int(data.get("version", CONFIG_VERSION))
    cfg.backend = data.get("backend", "hdr")
    cfg.profile = data.get("profile", "")
    if data.get("target"):
        cfg.target = target_from_dict(data["target"])
    for attr in ("record", "audio", "ui", "paths"):
        section = data.get(attr)
        if isinstance(section, dict):
            obj = getattr(cfg, attr)
            for k, v in section.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
    return cfg


def save_config(cfg: AppConfig, path: Path | str | None = None) -> Path:
    p = Path(path) if path else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 配置档
# --------------------------------------------------------------------------- #


def profiles_dir(root: Path | str | None = None) -> Path:
    return Path(root) if root else config_dir() / "profiles"


def profile_path(name: str, root: Path | str | None = None) -> Path:
    return profiles_dir(root) / f"{name}.json"


def _threshold_from_dict(data: dict[str, Any]) -> KeyThreshold:
    """配置档里的阈值 → `KeyThreshold`；`weights` 是 JSON 数组，转回 tuple。"""
    kwargs = dict(data)
    weights = kwargs.get("weights")
    if weights is not None:
        try:
            kwargs["weights"] = tuple(float(v) for v in weights)
        except (TypeError, ValueError):
            kwargs.pop("weights", None)
    return KeyThreshold(**kwargs)


def load_profile(name: str, root: Path | str | None = None) -> Profile:
    data = json.loads(profile_path(name, root).read_text(encoding="utf-8"))
    keygrid = None
    if data.get("keygrid"):
        from .types import KeyPoint

        pts = tuple(KeyPoint(u=float(p[0]), v=float(p[1]), r=float(p[2])) for p in data["keygrid"]["points"])
        keygrid = KeyGrid(points=pts, key_count=len(pts))
    thresholds = {
        code: _threshold_from_dict(t) for code, t in (data.get("thresholds") or {}).items()
    }
    baseline = None
    raw_baseline = data.get("baseline")
    if isinstance(raw_baseline, (list, tuple)) and raw_baseline:
        try:
            baseline = tuple(
                (int(row[0]), int(row[1]), int(row[2])) for row in raw_baseline
            )
        except (TypeError, ValueError, IndexError):
            baseline = None
    return Profile(
        name=data["name"],
        target=target_from_dict(data["target"]),
        instrument=data["instrument"],
        backend=data.get("backend", "auto"),
        ref_size=tuple(data.get("ref_size", (1920, 1080))),  # type: ignore[arg-type]
        keygrid=keygrid,
        thresholds=thresholds,
        baseline=baseline,
        created=data.get("created", ""),
    )


def save_profile(profile: Profile, root: Path | str | None = None) -> Path:
    p = profile_path(profile.name, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p
