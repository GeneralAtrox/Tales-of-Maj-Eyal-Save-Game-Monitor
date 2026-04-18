from __future__ import annotations

import builtins
import queue
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

if TYPE_CHECKING:
    from models import AppConfig


class LogBridge(QObject):
    """Captures print() / stderr output from monitor threads and relays it to the UI.

    Install with ``bridge.install()`` to redirect sys.stdout and sys.stderr.
    A 50 ms QTimer drains the internal queue on the main thread, so emitting
    ``message_ready`` is always main-thread-safe.
    """

    message_ready: Signal = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._queue: queue.Queue[str] = queue.Queue()
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(50)
        self._drain_timer.timeout.connect(self._drain)
        self._drain_timer.start()

    def install(self) -> None:
        sys.stdout = self  # type: ignore[assignment]
        sys.stderr = self  # type: ignore[assignment]

    def uninstall(self) -> None:
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        if self._drain_timer.isActive():
            self._drain_timer.stop()
        try:
            self._drain_timer.timeout.disconnect(self._drain)
        except (RuntimeError, TypeError):
            pass

    # ── stdout / stderr protocol ──────────────────────────────────────────
    def write(self, text: str) -> int:
        stripped = text.rstrip("\n")
        if stripped:
            self._queue.put(stripped)
        self._original_stdout.write(text)
        return len(text)

    def flush(self) -> None:
        self._original_stdout.flush()
        self._original_stderr.flush()

    def _drain(self) -> None:
        try:
            while True:
                self.message_ready.emit(self._queue.get_nowait())
        except queue.Empty:
            pass
        except RuntimeError:
            pass


class InputBridge(QObject):
    """Routes ``input()`` calls from monitor threads to a GUI dialog.

    The monitor thread calls ``request(prompt)`` which blocks until the GUI
    slot calls ``provide(text)``.  Connect ``input_needed`` to a slot that
    shows a QInputDialog and then calls ``provide``.
    """

    input_needed: Signal = Signal(str)  # emits the prompt string

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._event = threading.Event()
        self._result = ""

    def request(self, prompt: str) -> str:
        """Block the calling thread until the GUI provides a response."""
        self._event.clear()
        self._result = ""
        self.input_needed.emit(prompt)
        self._event.wait()
        return self._result

    def provide(self, text: str) -> None:
        """Called from the main thread to unblock ``request``."""
        self._result = text
        self._event.set()


class MonitorThread(threading.Thread):
    """Runs initialize_system + monitor_saves in a background daemon thread.

    While running, any ``input()`` call is routed through ``input_bridge``
    so the GUI can intercept it.  The config becomes readable via the
    ``config`` property once ``initialize_system`` completes.
    """

    def __init__(self, config_path: Path, input_bridge: InputBridge) -> None:
        super().__init__(daemon=True, name="monitor-worker")
        self.config_path = config_path
        self.input_bridge = input_bridge
        self._config: AppConfig | None = None

    @property
    def config(self) -> AppConfig | None:
        return self._config

    def run(self) -> None:
        original_input = builtins.input
        builtins.input = self.input_bridge.request  # type: ignore[assignment]
        try:
            from monitor import initialize_system, monitor_saves

            self._config = initialize_system(self.config_path)
            monitor_saves(self._config)
        finally:
            builtins.input = original_input
