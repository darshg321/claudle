"""Desktop presence: a mandatory tray icon and a small live task HUD.

The bot must never run invisibly, so `DesktopUI.start` raises if the tray
cannot be shown and the caller is expected to abort.

Threading: pystray needs its own thread with its own message loop and is not
safe to touch from outside it; the HUD is a separate process fed over a pipe.
The UI calls back out through plain callables that the bot marshals onto the
asyncio loop.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
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
    """Small always-on-top panel in the bottom-right corner, shown while busy.

    The panel is its own short-lived process (`python ui.py --hud`), started
    when a task begins and ended when it finishes, so an idle bot holds no Tk
    interpreter and runs no polling loop. Tk cannot be safely torn down and
    rebuilt inside one process across threads, and a separate process also
    means a Tk crash can never take the bot with it.

    Protocol: the bot writes `kind\tpayload` lines to the child's stdin; the
    child prints `kill` when its button is pressed and exits at stdin EOF.
    """

    def __init__(self, on_kill: Callable[[], None]):
        self._on_kill = on_kill
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        """Nothing to do until the first task; kept so DesktopUI reads the same."""

    def _spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--hud"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        def listen() -> None:
            for line in proc.stdout:
                if line.strip() == "kill":
                    self._on_kill()

        threading.Thread(target=listen, name="claudle-hud", daemon=True).start()
        return proc

    def send(self, kind: str, payload: str = "") -> None:
        with self._lock:
            proc = self._proc
            if kind in ("hide", "stop"):
                self._proc = None
                if proc is not None:
                    try:
                        proc.stdin.close()  # EOF: the panel closes itself
                    except OSError:
                        pass
                return
            if proc is None or proc.poll() is not None:
                if kind != "show":
                    return  # no panel up: an update has nothing to act on
                proc = self._proc = self._spawn()
            try:
                proc.stdin.write(f"{kind}\t{' '.join(payload.split())}\n")
                proc.stdin.flush()
            except OSError:
                self._proc = None


def _hud_main() -> None:
    """Entry point of the panel process. See _Hud."""
    import tkinter as tk

    width, height, margin, taskbar = 340, 112, 24, 56
    inbox: queue.Queue[tuple[str, str]] = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            kind, _, payload = line.rstrip("\n").partition("\t")
            inbox.put((kind, payload))
        inbox.put(("stop", ""))

    threading.Thread(target=read_stdin, daemon=True).start()

    # Tk's screen size spans every monitor on X11, which would put the panel
    # on whichever display sits furthest right. Anchor to the primary instead.
    try:
        import screen

        mon = screen.primary_monitor()
        primary = (mon["left"], mon["top"], mon["width"], mon["height"])
    except Exception:
        primary = None

    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.96)
    root.configure(bg=BG)

    frame = tk.Frame(root, bg=BG, padx=12, pady=10, highlightthickness=1)
    frame.configure(highlightbackground="#3f4147", highlightcolor="#3f4147")
    frame.pack(fill="both", expand=True)

    title = tk.Label(
        frame, text=f"{APP_NAME} — working", bg=BG, fg=FG,
        font=("Segoe UI", 9, "bold"), anchor="w",
    )
    title.pack(fill="x")

    body = tk.Label(
        frame, text="starting…", bg=BG, fg=MUTED, font=("Segoe UI", 8),
        anchor="w", justify="left", wraplength=width - 40,
    )
    body.pack(fill="x", pady=(4, 8))

    def kill() -> None:
        print("kill", flush=True)

    tk.Button(
        frame, text="Kill", command=kill, bg=DANGER, fg="white",
        activebackground="#a12828", activeforeground="white",
        relief="flat", font=("Segoe UI", 8, "bold"), padx=14, pady=2,
        cursor="hand2", borderwidth=0,
    ).pack(anchor="e")

    if primary is None:
        primary = (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())

    def pump() -> None:
        try:
            while True:
                kind, payload = inbox.get_nowait()
                if kind == "show":
                    title.configure(text=payload)
                    body.configure(text="starting…")
                    left, top, screen_w, screen_h = primary
                    x = left + screen_w - width - margin
                    y = top + screen_h - height - taskbar
                    root.geometry(f"{width}x{height}+{x}+{y}")
                    root.deiconify()
                    root.attributes("-topmost", True)
                elif kind == "title":
                    title.configure(text=payload)
                elif kind == "update":
                    body.configure(text=payload)
                elif kind == "stop":
                    root.destroy()
                    return
        except queue.Empty:
            pass
        root.after(120, pump)

    pump()
    root.mainloop()


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


if __name__ == "__main__" and "--hud" in sys.argv:
    _hud_main()
