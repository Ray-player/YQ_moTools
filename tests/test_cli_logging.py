"""CLI 在同一进程内被多次调用时的日志流处理（QA 报的回归）。

背景：`setup_logging()` 是幂等的全局配置，`logging.StreamHandler` 在构造时就把
`sys.stderr` 绑死了。第二次调用 `main()` 时若旧流已关闭（pytest capsys 用例结束、
Qt 嵌入、把 CLI 当库调用），logging 会在 emit 时打出 `--- Logging error ---` + traceback，
破坏"CLI 不出 traceback"的验收口径。`cli._DynamicStderrHandler` 在每次 emit 前重新解析
`sys.stderr`，从根上避免持有过期流引用。
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

from yqrec import cli


def test_second_main_call_rebinds_console_stream(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)

    first = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", first)
        assert cli.main(["version"]) == 0
    first.close()  # 模拟 capsys 在用例结束后关流

    second = io.StringIO()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stderr", second)
        code = cli.main(
            ["record", "--source", "synth", "--duration", "0.3", "--out", str(tmp_path / "a.txt")]
        )
    assert code == 0

    text = second.getvalue()
    assert "--- Logging error ---" not in text, text
    assert "Traceback" not in text, text
    assert "录制结束" in text, "第二次调用应把日志写到新的 stderr"
    capsys.readouterr()


def test_console_handler_is_dynamic_and_idempotent(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """控制台 handler 只留一个（幂等）、每次都写当前 stderr，且不动文件 handler。"""
    root = logging.getLogger("yqrec")
    file_handler = logging.FileHandler(tmp_path / "yqrec.log", encoding="utf-8")
    root.addHandler(file_handler)
    try:
        cli._install_console_handler(root)  # noqa: SLF001 - 直接验证内部钩子
        cli._install_console_handler(root)  # 幂等：不应叠加第二个控制台 handler
        console = [h for h in root.handlers if isinstance(h, cli._DynamicStderrHandler)]
        assert len(console) == 1
        assert file_handler in root.handlers, "文件 handler 不能被摘掉"

        fake = io.StringIO()
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stderr", fake)
            logging.getLogger("yqrec").warning("动态流可见")
        assert "动态流可见" in fake.getvalue()
    finally:
        root.removeHandler(file_handler)
        file_handler.close()
        capsys.readouterr()
