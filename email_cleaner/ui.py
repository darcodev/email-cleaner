"""Terminal output helpers: colors, tables, prompts, progress.

Colors turn themselves off when output isn't a terminal, when NO_COLOR
is set, or when the console can't do ANSI.
"""

from __future__ import annotations

import os
import shutil
import sys


def _harden_streams():
    # On Windows a redirected stdout can end up on a legacy code page that
    # chokes on non-ascii subject lines. Replace instead of crashing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_harden_streams()


def _windows_ansi_ok() -> bool:
    """Enable ANSI escape codes on Windows 10+ consoles."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        )
    except Exception:
        return False


def colors_enabled() -> bool:
    if os.environ.get("NO_COLOR") is not None:
        return False
    if not sys.stdout.isatty():
        return False
    return _windows_ansi_ok()


_ENABLED = colors_enabled()


def disable_colors():
    global _ENABLED
    _ENABLED = False


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"


def paint(text: str, *styles: str) -> str:
    if not _ENABLED or not styles:
        return text
    return "".join(styles) + text + Style.RESET


def info(msg: str):
    print(msg)


def ok(msg: str):
    print(paint(msg, Style.GREEN))


def warn(msg: str):
    print(paint("warning: " + msg, Style.YELLOW))


def error(msg: str, hint: str | None = None):
    print(paint("error: " + msg, Style.RED), file=sys.stderr)
    if hint:
        print("  hint: " + hint, file=sys.stderr)


def heading(msg: str):
    print()
    print(paint(msg, Style.BOLD))


def human_size(num_bytes: int) -> str:
    """1234567 -> '1.2 MB'"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def term_width() -> int:
    return shutil.get_terminal_size(fallback=(100, 24)).columns


def table(headers: list[str], rows: list[list[str]], max_width: int | None = None) -> str:
    """Simple aligned table that fits the terminal. The widest colum gets
    squeezed until it fits, so long subjects truncate instead of wrapping."""
    if not rows:
        return ""
    max_width = max_width or term_width()
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(row[i]))

    sep = "  "

    def total() -> int:
        return sum(widths) + len(sep) * (cols - 1)

    while total() > max_width and max(widths) > 8:
        widths[widths.index(max(widths))] -= 1

    def fmt(row: list[str]) -> str:
        cells = [truncate(row[i], widths[i]).ljust(widths[i]) for i in range(cols)]
        return sep.join(cells).rstrip()

    out = [paint(fmt(headers), Style.BOLD)]
    out.append(paint("-" * min(total(), max_width), Style.DIM))
    out.extend(fmt(row) for row in rows)
    return "\n".join(out)


def progress(done: int, total: int, label: str):
    """Single line progress counter that overwrites itself."""
    if total <= 0:
        return
    pct = int(done * 100 / total)
    msg = f"\r  {label} {done:,}/{total:,} ({pct}%)"
    sys.stdout.write(truncate(msg, term_width() - 1))
    sys.stdout.flush()
    if done >= total:
        sys.stdout.write("\n")


def confirm(question: str, danger: bool = False) -> bool:
    """Yes/no question, defaults to no. With danger=True the user has to
    type out the whole word 'yes'."""
    if danger:
        answer = input(paint(f"{question} Type 'yes' to continue: ", Style.YELLOW)).strip().lower()
        return answer == "yes"
    answer = input(f"{question} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or (default or "")
