"""tmux message types — transcribed from tmux 3.7b tmux-protocol.h.

Do not renumber. The gaps and explicit anchors (12, 100, 200, 300) are part of
the wire protocol.
"""

from enum import IntEnum


class Msg(IntEnum):
    VERSION = 12

    IDENTIFY_FLAGS = 100
    IDENTIFY_TERM = 101
    IDENTIFY_TTYNAME = 102
    IDENTIFY_OLDCWD = 103
    IDENTIFY_STDIN = 104
    IDENTIFY_ENVIRON = 105
    IDENTIFY_DONE = 106
    IDENTIFY_CLIENTPID = 107
    IDENTIFY_CWD = 108
    IDENTIFY_FEATURES = 109
    IDENTIFY_STDOUT = 110
    IDENTIFY_LONGFLAGS = 111
    IDENTIFY_TERMINFO = 112

    COMMAND = 200
    DETACH = 201
    DETACHKILL = 202
    EXIT = 203
    EXITED = 204
    EXITING = 205
    LOCK = 206
    READY = 207
    RESIZE = 208
    SHELL = 209
    SHUTDOWN = 210
    OLDSTDERR = 211
    OLDSTDIN = 212
    OLDSTDOUT = 213
    SUSPEND = 214
    UNLOCK = 215
    WAKEUP = 216
    EXEC = 217
    FLAGS = 218

    READ_OPEN = 300
    READ = 301
    READ_DONE = 302
    WRITE_OPEN = 303
    WRITE = 304
    WRITE_READY = 305
    WRITE_CLOSE = 306
    READ_CANCEL = 307


#: Only these two carry a file descriptor via SCM_RIGHTS (server-client.c
#: calls imsg_get_fd() for exactly these, and requires a zero-length payload).
FD_BEARING = frozenset({Msg.IDENTIFY_STDIN, Msg.IDENTIFY_STDOUT})

# Client flags (tmux.h). Sent in MSG_IDENTIFY_FLAGS.
CLIENT_TERMINAL = 0x1        #: attaching with a real tty (vs. a one-shot command)
CLIENT_NOSTARTSERVER = 0x1000
CLIENT_CONTROL = 0x2000      #: control mode (-C / -CC)
CLIENT_STARTSERVER = 0x10000000
