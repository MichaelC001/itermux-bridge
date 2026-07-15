"""Terminal handling for the fd a tmux client hands us.

The design note assumed the client puts its own terminal into raw mode. It does
not: client.c only calls cfmakeraw() for control mode (-CC). On a normal attach
the *server* owns the tty — tty.c does tcgetattr/tcsetattr on the fd received
over SCM_RIGHTS. We must do the same, or the line discipline eats our keys
(cooked mode: local echo, line buffering, Ctrl-C raising SIGINT client-side).

Flags below are transcribed from tty_start_tty() in tmux 3.7b tty.c.
"""

import fcntl
import os
import struct
import termios
from typing import Optional, Tuple


class ClientTTY:
    """Owns the raw-mode transition for one attached client's terminal."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._saved: Optional[list] = None

    def is_tty(self) -> bool:
        return os.isatty(self.fd)

    def start(self) -> None:
        """Put the terminal into the same raw-ish mode tmux uses."""
        if not self.is_tty():
            return
        self._saved = termios.tcgetattr(self.fd)
        tio = termios.tcgetattr(self.fd)

        # iflag: drop flow control, CR/NL translation, bell, 8th-bit strip
        tio[0] &= ~(termios.IXON | termios.IXOFF | termios.ICRNL |
                    termios.INLCR | termios.IGNCR | termios.IMAXBEL |
                    termios.ISTRIP)
        tio[0] |= termios.IGNBRK
        # oflag: no post-processing (we emit exact bytes)
        tio[1] &= ~(termios.OPOST | termios.ONLCR | termios.OCRNL |
                    termios.ONLRET)
        # lflag: no canonical mode, no echo, no signal generation
        tio[3] &= ~(termios.IEXTEN | termios.ICANON | termios.ECHO |
                    termios.ECHOE | termios.ECHONL | termios.ECHOCTL |
                    termios.ECHOPRT | termios.ECHOKE | termios.ISIG)
        # read() returns as soon as 1 byte is available
        tio[6][termios.VMIN] = 1
        tio[6][termios.VTIME] = 0

        termios.tcsetattr(self.fd, termios.TCSANOW, tio)
        termios.tcflush(self.fd, termios.TCOFLUSH)

    def stop(self) -> None:
        """Restore the terminal exactly as we found it, and tidy the screen."""
        if self._saved is None:
            return
        try:
            # Leave the alternate screen, stop mouse reporting, and reset
            # attributes before handing the terminal back — otherwise the user's
            # shell inherits our state and every mouse move spews escape codes
            # into their prompt.
            os.write(self.fd,
                     b"\033[?1006l\033[?1002l\033[?1000l"   # mouse off
                     b"\033[?1049l\033[?25h\033[m\r")       # main screen, cursor, SGR
        except OSError:
            pass
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self._saved)
        except (termios.error, OSError):
            pass
        self._saved = None

    def size(self) -> Tuple[int, int]:
        """(cols, rows) straight from the kernel — TIOCGWINSZ on the passed fd.

        More reliable than parsing MSG_RESIZE payloads, and it's what tty.c does.
        """
        try:
            packed = fcntl.ioctl(self.fd, termios.TIOCGWINSZ, b"\0" * 8)
            rows, cols, _, _ = struct.unpack("HHHH", packed)
            if rows and cols:
                return cols, rows
        except OSError:
            pass
        return 80, 24
