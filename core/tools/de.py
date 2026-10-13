"""Рабочий стол: один слой для всего, что зависит от оболочки (переезд на Ubuntu Cinnamon, X11 — 2026-10-13).

Раньше инструменты звали KDE напрямую (kscreen-doctor, spectacle, qdbus6 KWin, wl-copy). В Cinnamon их нет,
и Ксения без звука теряла бы экран, буфер обмена, лупу, мышь, тему. Здесь — X11/Cinnamon по умолчанию,
KDE (Wayland) — запасной путь, если проект снова окажется в Plasma.
"""
import os
import re
import shutil
import subprocess


def _run(*args, timeout=10, input_bytes=None):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, input=input_bytes)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return -1, b""


def kde() -> bool:
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "") and bool(shutil.which("kscreen-doctor"))


def wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


# ---------- монитор (DPMS) ----------
def dpms_is_off() -> bool:
    if kde():
        return b"off" in _run("kscreen-doctor", "--dpms", "show", timeout=5)[1]
    return b"Monitor is Off" in _run("xset", "q", timeout=5)[1]


def dpms(on: bool):
    if kde():
        _run("kscreen-doctor", "--dpms", "on" if on else "off", timeout=5)
    else:
        _run("xset", "dpms", "force", "on" if on else "off", timeout=5)


# ---------- снимок экрана ----------
def screenshot(path: str, window: bool = False) -> bool:
    if os.path.exists(path):
        os.remove(path)
    if kde():
        _run("spectacle", "-b", "-n", "-a" if window else "-f", "-o", path, timeout=20)
    else:
        _run("gnome-screenshot", *(["-w"] if window else []), "-f", path, timeout=20)
        if not os.path.exists(path) and not window:
            try:  # запасной путь X11 без программ
                from PIL import ImageGrab
                ImageGrab.grab().save(path)
            except Exception:
                pass
    return os.path.exists(path)


# ---------- буфер обмена ----------
def clip_get(primary: bool = False) -> bytes:
    if wayland():
        return _run("wl-paste", "--no-newline", *(["--primary"] if primary else []), timeout=5)[1]
    return _run("xclip", "-o", "-selection", "primary" if primary else "clipboard", timeout=5)[1]


def clip_set(data: bytes) -> bool:
    if wayland():
        return _run("wl-copy", input_bytes=data, timeout=5)[0] == 0
    # xclip держит буфер сам, пока его не перехватят: запускаем отдельно и не ждём
    try:
        p = subprocess.Popen(["xclip", "-selection", "clipboard"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        p.stdin.write(data)
        p.stdin.close()
        return True
    except OSError:
        return False


# ---------- экранная лупа ----------
ZOOM_STEP = os.path.expanduser("~/.local/bin/zoom-step")  # шаг лупы Александра (×2 за нажатие), см. заметку о переезде


def zoom_active() -> bool:
    if kde():
        return "zoom" in _run("qdbus6", "org.kde.KWin", "/Effects", "org.kde.kwin.Effects.activeEffects",
                              timeout=3)[1].decode(errors="replace").split()
    out = _run("gsettings", "get", "org.cinnamon.desktop.a11y.applications", "screen-magnifier-enabled")[1]
    return out.strip() == b"true"


def zoom(step_in: bool) -> bool:
    if kde():
        return _run("qdbus6", "org.kde.kglobalaccel", "/component/kwin", "org.kde.kglobalaccel.Component.invokeShortcut",
                    "view_zoom_in" if step_in else "view_zoom_out")[0] == 0
    if os.path.exists(ZOOM_STEP):
        return _run(ZOOM_STEP, "in" if step_in else "out")[0] == 0
    return False


# ---------- указатель и окна (X11) ----------
def cursor_pos():
    out = _run("xdotool", "getmouselocation", "--shell", timeout=3)[1].decode()
    x, y = re.search(r"X=(\d+)", out), re.search(r"Y=(\d+)", out)
    return (int(x.group(1)), int(y.group(1))) if x and y else None


def mouse_to(x: int, y: int) -> bool:
    return _run("xdotool", "mousemove", str(int(x)), str(int(y)), timeout=3)[0] == 0


def window_action(action: str) -> bool:
    """close | minimize | maximize — для активного окна."""
    if kde():
        name = {"close": "Window Close", "minimize": "Window Minimize", "maximize": "Window Maximize"}[action]
        return _run("qdbus6", "org.kde.kglobalaccel", "/component/kwin",
                    "org.kde.kglobalaccel.Component.invokeShortcut", name)[0] == 0
    if action == "close":
        return _run("xdotool", "getactivewindow", "windowclose")[0] == 0 or \
            _run("wmctrl", "-c", ":ACTIVE:")[0] == 0
    if action == "minimize":
        return _run("xdotool", "getactivewindow", "windowminimize")[0] == 0
    return _run("wmctrl", "-r", ":ACTIVE:", "-b", "toggle,maximized_vert,maximized_horz")[0] == 0


def app_menu() -> bool:
    if kde():
        return _run("qdbus6", "org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell.activateLauncherMenu",
                    timeout=6)[0] == 0
    # меню Cinnamon открывается клавишей Super (как у Александра в Windows)
    return _run("xdotool", "key", "super", timeout=3)[0] == 0
