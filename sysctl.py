"""Mouse/keyboard control, process management, clipboard, volume and power.

Everything here is blocking; call through asyncio.to_thread.
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime, timedelta

import psutil

_pyautogui = None


def gui():
    """Lazily import pyautogui (it needs an interactive desktop session)."""
    global _pyautogui
    if _pyautogui is None:
        import pyautogui

        pyautogui.FAILSAFE = False  # a corner-parked cursor must not kill the bot
        pyautogui.PAUSE = 0.02
        _pyautogui = pyautogui
    return _pyautogui


# --------------------------------------------------------------------------- input


def move(x: int, y: int, duration: float = 0.15) -> None:
    gui().moveTo(x, y, duration=duration)


def click(x: int | None = None, y: int | None = None, button: str = "left", clicks: int = 1) -> None:
    g = gui()
    if x is None or y is None:
        g.click(button=button, clicks=clicks, interval=0.08)
    else:
        g.click(x=x, y=y, button=button, clicks=clicks, interval=0.08)


def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
    g = gui()
    g.moveTo(x1, y1, duration=0.15)
    g.dragTo(x2, y2, duration=duration, button="left")


def scroll(amount: int, x: int | None = None, y: int | None = None) -> None:
    if x is not None and y is not None:
        gui().moveTo(x, y, duration=0.1)
    gui().scroll(amount)


def type_text(text: str, interval: float = 0.01) -> None:
    gui().write(text, interval=interval)


def press_keys(combo: str) -> list[str]:
    """`ctrl+shift+esc` -> hotkey; `enter` -> single press. Space-separated = sequence."""
    g = gui()
    pressed: list[str] = []
    for chunk in combo.split():
        keys = [k.strip().lower() for k in chunk.split("+") if k.strip()]
        if not keys:
            continue
        if len(keys) == 1:
            g.press(keys[0])
        else:
            g.hotkey(*keys)
        pressed.append("+".join(keys))
        time.sleep(0.05)
    return pressed


def cursor_position() -> tuple[int, int]:
    pos = gui().position()
    return int(pos.x), int(pos.y)


def screen_size() -> tuple[int, int]:
    size = gui().size()
    return int(size.width), int(size.height)


# ----------------------------------------------------------------------- clipboard


def clipboard_get() -> str:
    import pyperclip

    return pyperclip.paste()


def clipboard_set(text: str) -> None:
    import pyperclip

    pyperclip.copy(text)


# ------------------------------------------------------------------------ processes


def list_processes(name_filter: str = "", limit: int = 25) -> list[dict]:
    needle = name_filter.lower()
    rows = []
    for proc in psutil.process_iter(["pid", "name", "memory_info", "username"]):
        try:
            info = proc.info
            name = info.get("name") or "?"
            if needle and needle not in name.lower():
                continue
            mem = info.get("memory_info")
            rows.append(
                {
                    "pid": info["pid"],
                    "name": name,
                    "mem_mb": (mem.rss / 1048576) if mem else 0.0,
                    "user": (info.get("username") or "").split("\\")[-1],
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows.sort(key=lambda r: r["mem_mb"], reverse=True)
    return rows[:limit]


def kill_process(target: str, force: bool = False) -> list[str]:
    """Target is a PID or a process name. Returns human-readable results."""
    results: list[str] = []
    if target.isdigit():
        candidates = [psutil.Process(int(target))]
    else:
        needle = target.lower()
        if not needle.endswith(".exe"):
            needle_alt = needle + ".exe"
        else:
            needle_alt = needle
        candidates = [
            p
            for p in psutil.process_iter(["name"])
            if (p.info.get("name") or "").lower() in (needle, needle_alt)
        ]
    if not candidates:
        return [f"no process matched {target!r}"]

    for proc in candidates:
        try:
            name = proc.name()
            pid = proc.pid
            proc.kill() if force else proc.terminate()
            results.append(f"{'killed' if force else 'terminated'} {name} (pid {pid})")
        except psutil.NoSuchProcess:
            results.append(f"pid {proc.pid} already gone")
        except psutil.AccessDenied:
            results.append(f"access denied for pid {proc.pid} (needs admin)")
    return results


# --------------------------------------------------------------------------- system


def system_info() -> dict:
    vm = psutil.virtual_memory()
    boot = datetime.fromtimestamp(psutil.boot_time())
    info = {
        "host": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": f"{psutil.cpu_percent(interval=0.4):.0f}% over {psutil.cpu_count()} threads",
        "ram": f"{vm.used / 1073741824:.1f} / {vm.total / 1073741824:.1f} GiB ({vm.percent:.0f}%)",
        "uptime": str(timedelta(seconds=int(time.time() - psutil.boot_time()))),
        "booted": boot.strftime("%Y-%m-%d %H:%M"),
    }
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append(
            f"{part.device.rstrip(chr(92))} {usage.used / 1073741824:.0f}"
            f"/{usage.total / 1073741824:.0f} GiB ({usage.percent:.0f}%)"
        )
    info["disks"] = "  |  ".join(disks) or "n/a"

    battery = psutil.sensors_battery()
    if battery:
        state = "charging" if battery.power_plugged else "on battery"
        info["battery"] = f"{battery.percent:.0f}% ({state})"
    return info


# --------------------------------------------------------------------------- volume

_VK_VOLUME_MUTE = 0xAD
_VK_VOLUME_DOWN = 0xAE
_VK_VOLUME_UP = 0xAF
_KEYEVENTF_KEYUP = 0x0002


def _tap(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
    time.sleep(0.005)


def volume_mute_toggle() -> None:
    _tap(_VK_VOLUME_MUTE)


def volume_step(steps: int) -> None:
    """Each step is roughly 2% on Windows."""
    vk = _VK_VOLUME_UP if steps > 0 else _VK_VOLUME_DOWN
    for _ in range(min(abs(steps), 50)):
        _tap(vk)


def volume_set(percent: int) -> int:
    """Absolute level by driving the volume keys down to zero and back up."""
    percent = max(0, min(100, percent))
    for _ in range(50):
        _tap(_VK_VOLUME_DOWN)
    for _ in range(round(percent / 2)):
        _tap(_VK_VOLUME_UP)
    return percent


# ---------------------------------------------------------------------------- power


def lock_workstation() -> None:
    ctypes.windll.user32.LockWorkStation()


def sleep_pc() -> None:
    subprocess.Popen(
        ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def shutdown(delay_seconds: int = 60) -> str:
    subprocess.run(["shutdown", "/s", "/t", str(delay_seconds)], check=True)
    return f"shutdown scheduled in {delay_seconds}s — `{'!'}abort` cancels it"


def reboot(delay_seconds: int = 60) -> str:
    subprocess.run(["shutdown", "/r", "/t", str(delay_seconds)], check=True)
    return f"reboot scheduled in {delay_seconds}s"


def abort_shutdown() -> str:
    result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True)
    if result.returncode == 0:
        return "pending shutdown cancelled"
    return (result.stderr or result.stdout or "nothing to cancel").strip()


# ----------------------------------------------------------------------------- misc


def open_target(target: str, cwd: str | None = None) -> str:
    """Open a URL, file, folder or application the way Explorer would."""
    if target.startswith(("http://", "https://", "mailto:")):
        os.startfile(target)
        return f"opened URL {target}"

    candidate = os.path.expandvars(os.path.expanduser(target))
    if cwd and not os.path.isabs(candidate) and os.path.exists(os.path.join(cwd, candidate)):
        candidate = os.path.join(cwd, candidate)

    if os.path.exists(candidate):
        os.startfile(candidate)
        return f"opened {candidate}"

    resolved = shutil.which(target)
    if resolved:
        subprocess.Popen([resolved], creationflags=subprocess.CREATE_NO_WINDOW)
        return f"launched {resolved}"

    # Last resort: let the shell resolve it (handles `notepad`, `calc`, ms-settings:, ...)
    subprocess.Popen(["cmd.exe", "/c", "start", "", target], creationflags=subprocess.CREATE_NO_WINDOW)
    return f"asked Windows to start {target}"


CHROME_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)


def launch_browser(profile_dir: str = "", url: str = "") -> str:
    """Open Chrome so the owner can sign into the sites Claude will browse.

    With a profile dir, that is deliberately the same --user-data-dir the MCP
    server uses: cookies set there are the cookies Claude browses with. Empty
    means the normal Chrome, which is what the Claude in Chrome extension drives.
    """
    exe = shutil.which("chrome")
    if not exe:
        for candidate in CHROME_CANDIDATES:
            expanded = os.path.expandvars(candidate)
            if os.path.exists(expanded):
                exe = expanded
                break
    if not exe:
        raise FileNotFoundError("Could not find chrome.exe — install Chrome or add it to PATH.")

    args = [exe]
    if profile_dir:
        args += [f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check"]
    if url:
        args.append(url)
    subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)
    if profile_dir:
        return f"opened Chrome on the automation profile ({profile_dir})"
    return "opened your normal Chrome"


def list_windows() -> list[str]:
    """Titles of visible top-level windows."""
    user32 = ctypes.windll.user32
    titles: list[str] = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value.strip():
            titles.append(buf.value)
        return True

    user32.EnumWindows(EnumWindowsProc(callback), None)
    return titles


def focus_window(needle: str) -> str:
    """Bring the first window whose title contains `needle` to the foreground."""
    user32 = ctypes.windll.user32
    target = {"hwnd": None, "title": ""}
    lowered = needle.lower()

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
    )

    def callback(hwnd, _lparam):
        if target["hwnd"] is not None or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if lowered in buf.value.lower():
            target["hwnd"] = hwnd
            target["title"] = buf.value
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(callback), None)
    if target["hwnd"] is None:
        return f"no visible window matching {needle!r}"

    user32.ShowWindow(target["hwnd"], 9)  # SW_RESTORE
    user32.SetForegroundWindow(target["hwnd"])
    return f"focused {target['title']!r}"
