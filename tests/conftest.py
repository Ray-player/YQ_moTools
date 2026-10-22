"""共享 fixture。"""

from __future__ import annotations

import pytest

from yqrec.instruments import load_instrument


@pytest.fixture(scope="session")
def spec_fwzs():
    return load_instrument("fwzs")


@pytest.fixture(scope="session")
def spec_yy():
    return load_instrument("yy")


@pytest.fixture(scope="session")
def all_specs():
    from yqrec.instruments import load_instruments

    return load_instruments()
