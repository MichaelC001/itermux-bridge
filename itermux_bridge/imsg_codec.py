"""imsg frame encoding/decoding — a Python port of OpenBSD imsg.c framing as used by tmux.

Wire format (struct imsg_hdr, native endian, no padding on the platforms tmux runs on):

    uint32 type
    uint32 len      total frame length INCLUDING this 16-byte header
    uint32 peerid   tmux stores PROTOCOL_VERSION in the low byte (peerid & 0xff)
    uint32 pid

File descriptors ride out-of-band via SCM_RIGHTS, not in the payload.
"""

import struct
from typing import NamedTuple, Optional

HDR_FMT = "=IIII"
IMSG_HEADER_SIZE = struct.calcsize(HDR_FMT)  # 16
MAX_IMSGSIZE = 16384

#: The high bit of `len` is a flag, not part of the length: it means "an fd is
#: attached to this frame via SCM_RIGHTS". Straight from imsg.c:
#:     #define IMSG_FD_MARK 0x80000000U
#:     len = hdr.len & ~IMSG_FD_MARK;
#:     if (hdr.len & IMSG_FD_MARK) { ibuf_fd_set(b, *fd); *fd = -1; }
#: Miss this and every fd-bearing frame decodes as a ~2GB length and blows up.
IMSG_FD_MARK = 0x80000000

# tmux 3.7b (and 3.5a) both use 8. Verified against tmux-protocol.h.
PROTOCOL_VERSION = 8


class Msg(NamedTuple):
    type: int
    peerid: int
    pid: int
    payload: bytes
    fd: Optional[int] = None

    @property
    def version(self) -> int:
        """tmux encodes the protocol version in the low byte of peerid."""
        return self.peerid & 0xFF


def pack(msg_type: int, payload: bytes = b"", peerid: int = PROTOCOL_VERSION,
         pid: int = 0xFFFFFFFF, with_fd: bool = False) -> bytes:
    """Build a single imsg frame.

    Mirrors tmux's proc_send(), which always composes with
    imsg_compose(ibuf, type, PROTOCOL_VERSION, -1, fd, ...) — so peerid carries
    the version and pid is -1 (0xffffffff) for ordinary messages.

    Set with_fd when an fd accompanies this frame over SCM_RIGHTS; it flips
    IMSG_FD_MARK in the length field, which is how the peer knows to claim it.
    """
    total = IMSG_HEADER_SIZE + len(payload)
    if total > MAX_IMSGSIZE:
        raise ValueError(f"imsg too large: {total} > {MAX_IMSGSIZE}")
    if with_fd:
        total |= IMSG_FD_MARK
    return struct.pack(HDR_FMT, msg_type, total, peerid, pid) + payload


class Decoder:
    """Incremental frame decoder — feed it bytes, pull out whole messages.

    A stream read can split a frame anywhere, so buffer until a full frame
    (header says how long) has arrived.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._fds: list = []

    def feed(self, data: bytes, fds: Optional[list] = None) -> None:
        self._buf.extend(data)
        if fds:
            self._fds.extend(fds)

    def __iter__(self):
        return self

    def __next__(self) -> Msg:
        msg = self.next_msg()
        if msg is None:
            raise StopIteration
        return msg

    def next_msg(self) -> Optional[Msg]:
        """Pop one complete message, or None if we need more bytes."""
        if len(self._buf) < IMSG_HEADER_SIZE:
            return None

        msg_type, raw_len, peerid, pid = struct.unpack(
            HDR_FMT, bytes(self._buf[:IMSG_HEADER_SIZE])
        )
        has_fd = bool(raw_len & IMSG_FD_MARK)
        length = raw_len & ~IMSG_FD_MARK

        if length < IMSG_HEADER_SIZE or length > MAX_IMSGSIZE:
            raise ValueError(f"invalid imsg length {length} (type={msg_type})")
        if len(self._buf) < length:
            return None

        payload = bytes(self._buf[IMSG_HEADER_SIZE:length])
        del self._buf[:length]

        # Several frames can surface from one recvmsg() with their SCM_RIGHTS
        # fds pooled together, so we can't pair by arrival. The FD_MARK bit is
        # authoritative: fds attach, in order, to exactly the marked frames.
        fd = self._fds.pop(0) if (has_fd and self._fds) else None
        return Msg(msg_type, peerid, pid, payload, fd)
