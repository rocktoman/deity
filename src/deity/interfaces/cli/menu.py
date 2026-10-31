"""Small terminal selector for interactive slash commands."""

import os
import sys


def _move(index: int, direction: str, count: int) -> int:
    """Wrap arrow-key navigation around a nonempty menu."""
    return (index + (-1 if direction == "up" else 1)) % count


def _read_key() -> str:
    if os.name == "nt":
        import msvcrt

        read_character = getattr(msvcrt, "getwch")  # noqa: B009 - unavailable in Unix stubs
        key = str(read_character())
        if key in {"\x00", "\xe0"}:
            return {"H": "up", "P": "down"}.get(str(read_character()), "other")
    else:
        import select

        fd = sys.stdin.fileno()
        key = os.read(fd, 1).decode(errors="ignore")
        if key == "\x1b":
            if not select.select([fd], [], [], 0.05)[0]:
                return "escape"
            if os.read(fd, 1) == b"[":
                return {"A": "up", "B": "down"}.get(os.read(fd, 1).decode(errors="ignore"), "other")
            return "escape"
    if key in {"\r", "\n"}:
        return "enter"
    if key in {"\x03", "\x04", "\x1b"}:
        return "escape"
    return "other"


def choose(title: str, items: list[str]) -> int | None:
    """Select an item with arrow keys; accept a number when input is piped."""
    if not items:
        print(f"{title}：无可用项")
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print(title)
        for number, item in enumerate(items, 1):
            print(f"  {number}. {item}")
        try:
            answer = input("选择编号（回车取消）> ").strip()
        except EOFError:
            return None
        return int(answer) - 1 if answer.isdigit() and 1 <= int(answer) <= len(items) else None

    def draw(index: int, *, redraw: bool) -> None:
        lines = [title, *[f"{'❯' if i == index else ' '} {item}" for i, item in enumerate(items)]]
        lines.append("↑↓ 选择 · Enter 确认 · Esc 返回")
        if redraw:
            sys.stdout.write(f"\x1b[{len(lines)}F")
        ending = "\r\n" if os.name != "nt" else "\n"
        for line in lines:
            sys.stdout.write(f"\x1b[2K{line}{ending}")
        sys.stdout.flush()

    if os.name != "nt":
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setraw(fd)
    index = 0
    try:
        draw(index, redraw=False)
        while True:
            key = _read_key()
            if key == "enter":
                return index
            if key == "escape":
                return None
            if key in {"up", "down"}:
                index = _move(index, key, len(items))
                draw(index, redraw=True)
    finally:
        if os.name != "nt":
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
