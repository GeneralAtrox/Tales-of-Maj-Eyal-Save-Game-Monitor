from __future__ import annotations

import builtins
import sys
from typing import Any, TextIO


def _console_stream(*, stderr: bool) -> TextIO:
    stream = sys.__stderr__ if stderr else sys.__stdout__
    if stream is None:
        stream = sys.stderr if stderr else sys.stdout
    return stream


def console_print(*args: Any, stderr: bool = False, **kwargs: Any) -> None:
    file = kwargs.pop("file", None)
    builtins.print(*args, file=file or _console_stream(stderr=stderr), **kwargs)
