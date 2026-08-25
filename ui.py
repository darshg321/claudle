"""Desktop presence: a mandatory tray icon and a small live task HUD.

The bot must never run invisibly, so `DesktopUI.start` raises if the tray
cannot be shown and the caller is expected to abort.

Threading: pystray and tkinter each need their own thread with their own
message loop, and neither is safe to touch from outside its thread. The bot
pushes work in through queues, and the UI calls back out through plain
callables that the bot marshals onto the asyncio loop.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable

APP_NAME = "Claudle"

IDLE_COLOR = (88, 101, 242, 255)   # blurple, matching Discord
BUSY_COLOR = (240, 170, 60, 255)   # amber while a prompt is running

BG = "#1e1f22"
FG = "#dbdee1"
MUTED = "#949ba4"
DANGER = "#da373c"


class TrayUnavailable(RuntimeError):
    """The tray icon could not be created, so the bot must not start."""


def _icon_image(color: tuple[int, int, int, int]):
    """Draw the tray icon rather than shipping a binary asset."""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 4, size - 4), fill=color)
    draw.ellipse((22, 22, size - 22, size - 22), fill=(255, 255, 255, 235))
    return image


class _Tray:
    def __init__(self, on_quit: Callable[[], None], on_kill: Callable[[], None]):
        self._on_quit = on_quit
        self._on_kill = on_kill
        self._icon = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._busy = False
        self._status = "idle"

    def start(self, timeout: float = 15.0) -> None:
        try:
            import pystray
        except Exception as exc:  # noqa: BLE001 - any import failure is fatal here
            raise TrayUnavailable(f"pystray is not usable: {exc}") from exc

        menu = pystray.Menu(
            pystray.MenuItem(lambda _: f"{APP_NAME} — {self._status}", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Kill current task",
                lambda *_: self._on_kill(),
                enabled=lambda _: self._busy,
            ),
            pystray.MenuItem("Quit", lambda *_: self._on_quit()),
        )
        self._icon = pystray.Icon(
            "claudle", _icon_image(IDLE_COLOR), f"{APP_NAME} — idle", menu
        )

        def setup(icon) -> None:
            icon.visible = True
            self._ready.set()

        def run() -> None:
            try:
                self._icon.run(setup=setup)
            finally:
                self._ready.set()  # unblock start() so a failure surfaces as an error

        self._thread = threading.Thread(target=run, name="claudle-tray", daemon=True)
        self._thread.start()

        # Fail fast: no visible tray icon means no silent background bot. The
        # event also fires if run() died, so confirm the icon really is up.
        if not self._ready.wait(timeout):
            raise TrayUnavailable(f"the tray icon did not appear within {timeout:.0f}s")
        if not getattr(self._icon, "visible", False):
            raise TrayUnavailable("the tray backend exited immediately")

    def set_state(self, busy: bool, status: str) -> None:
        self._busy = busy
        self._status = status
        if not self._icon:
            return
        try:
            self._icon.icon = _icon_image(BUSY_COLOR if busy else IDLE_COLOR)
            self._icon.title = f"{APP_NAME} — {status}"[:127]
            self._icon.update_menu()
        except Exception:  # a cosmetic failure must never take the bot down
            pass

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None


class _Hud:
    """Small always-on-top panel in the bottom-right corner, shown while busy."""

    WIDTH = 340
    HEIGHT = 112
    MARGIN = 24
    TASKBAR = 56

    def __init__(self, on_kill: Callable[[], None]):
        self._on_kill = on_kill
        self._q: queue.Queue[tuple[str, str]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._root = None
        self._title = None
        self._body = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="claudle-hud", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.96)
        root.configure(bg=BG)

        frame = tk.Frame(root, bg=BG, padx=12, pady=10, highlightthickness=1)
        frame.configure(highlightbackground="#3f4147", highlightcolor="#3f4147")
        frame.pack(fill="both", expand=True)

        self._title = tk.Label(
            frame, text=f"{APP_NAME} — working", bg=BG, fg=FG,
            font=("Segoe UI", 9, "bold"), anchor="w",
        )
        self._title.pack(fill="x")

        self._body = tk.Label(
            frame, text="starting…", bg=BG, fg=MUTED, font=("Segoe UI", 8),
            anchor="w", justify="left", wraplength=self.WIDTH - 40,
        )
        self._body.pack(fill="x", pady=(4, 8))

        tk.Button(
            frame, text="Kill", command=self._on_kill, bg=DANGER, fg="white",
            activebackground="#a12828", activeforeground="white",
            relief="flat", font=("Segoe UI", 8, "bold"), padx=14, pady=2,
            cursor="hand2", borderwidth=0,
        ).pack(anchor="e")

        self._root = root
        self._pump()
        root.mainloop()

    def _place(self) -> None:
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = screen_w - self.WIDTH - self.MARGIN
        y = screen_h - self.HEIGHT - self.TASKBAR
        self._root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "show":
                    self._title.configure(text=payload)
                    self._body.configure(text="starting…")
                    self._place()
                    self._root.deiconify()
                    self._root.attributes("-topmost", True)
                elif kind == "title":
                    self._title.configure(text=payload)
                elif kind == "update":
                    self._body.configure(text=payload)
                elif kind == "hide":
                    self._root.withdraw()
                elif kind == "stop":
                    self._root.destroy()
                    return
        except queue.Empty:
            pass
        except Exception:
            pass
        self._root.after(120, self._pump)

    def send(self, kind: str, payload: str = "") -> None:
        self._q.put((kind, payload))


class DesktopUI:
    """Facade the bot talks to. Every method is safe from the asyncio thread."""

    def __init__(self) -> None:
        self._tray: _Tray | None = None
        self._hud: _Hud | None = None

    def start(self, on_quit: Callable[[], None], on_kill: Callable[[], None]) -> None:
        """Bring up tray and HUD. Raises TrayUnavailable if the tray fails."""
        self._tray = _Tray(on_quit, on_kill)
        self._tray.start()
        self._hud = _Hud(on_kill)
        self._hud.start()

    def task_started(self, title: str) -> None:
        if self._tray:
            self._tray.set_state(True, "running a prompt")
        if self._hud:
            self._hud.send("show", f"{APP_NAME} — {title}"[:70])

    def task_update(self, text: str) -> None:
        if self._hud:
            self._hud.send("update", " ".join(text.split())[:180])

    def task_finished(self, note: str = "idle") -> None:
        if self._tray:
            self._tray.set_state(False, note)
        if self._hud:
            self._hud.send("hide")

    def stop(self) -> None:
        if self._hud:
            self._hud.send("stop")
            self._hud = None
        if self._tray:
            self._tray.stop()
            self._tray = None
