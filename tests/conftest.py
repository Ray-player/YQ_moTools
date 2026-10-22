"""共享 fixture。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from yqrec.instruments import load_instrument


@pytest.fixture(scope="session")
def spec_fwzs():
    return load_instrument("fwzs")


@pytest.fixture(scope="session")
def spec_fwzs_single_frame(spec_fwzs):
    """确认帧数=1 的 fwzs（M4 起全局默认是 2）。

    给"单帧即确认 / 滞回 / 最小间隔"这类与确认帧数无关的老用例用，
    这样它们测的仍是原语义，不被新默认值殃及。
    """
    return replace(spec_fwzs, detection=replace(spec_fwzs.detection, confirm_frames=1))


@pytest.fixture(scope="session")
def spec_xljq():
    """谐律键琴（`sustain_ms = 300`）。"""
    return load_instrument("xljq")


@pytest.fixture(scope="session")
def spec_yy():
    return load_instrument("yy")


@pytest.fixture(scope="session")
def all_specs():
    from yqrec.instruments import load_instruments

    return load_instruments()
