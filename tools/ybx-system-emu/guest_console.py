"""Read-only guest console: tail the ESP32 UART output of a running QEMU.

The browser bridge starts QEMU with every serial port sent to ``null``
(``serve_x4pro_browser.py``: ``"-serial","null","-serial","null",...``), so the
guest's own boot ROM / bootloader / ESP-IDF log is invisible. A firmware image
that never paints a frame produces no evidence at all, only "0 frames".

This module attaches to a QEMU serial chardev and keeps the last ``capacity``
bytes (default 64 KiB) of the guest's output in a ring buffer, fed by a reader
thread. It never writes to QEMU: the chardev is read-only from the console's
side, and nothing here sends bytes back into the guest.

Two chardev backends are supported; pick one when starting QEMU:

* unix socket (RECOMMENDED, primary). Start QEMU with
  ``-serial unix:PATH,server=on,wait=off -serial null -serial null -serial null``.
  QEMU creates and listens on ``PATH``; ``UnixSocketChardevSource`` connects as
  a client and re-reads from the first byte the guest prints. ``wait=off``
  means QEMU boots even with no console attached; bytes printed while the
  console is disconnected are LOST. Replace only the FIRST ``-serial``; on the
  esp32s3 machine that is UART0, where the ROM and bootloader print.
* file. Start QEMU with
  ``-serial file:PATH -serial null -serial null -serial null``. QEMU truncates
  and appends to ``PATH``; ``FileChardevSource`` follows the growing file from
  the host side. Works across a Docker bind mount, where a unix socket
  reachable from the host is unreliable.

CLI for a quick manual look at a live guest::

    python3 guest_console.py --socket /tmp/guest0.sock --duration 30
    python3 guest_console.py --file /tmp/guest0.log --duration 30
    python3 guest_console.py --socket /tmp/guest0.sock --until 'esp_reset' --max-wait 90

Exit codes: 0 printed a tail; 2 the chardev never became connectable.
"""

from __future__ import annotations

import argparse
import errno
import os
import socket
import sys
import threading
import time

DEFAULT_CAPACITY = 64 * 1024
CHUNK = 4096
_POLL = 0.1  # retry cadence for both sources; keeps tests fast

# (offset of the first non-continuation byte, sequence length from its lead
# byte) for a UTF-8 tail that may still be waiting for continuation bytes.
_LEAD_LENGTH = {0x00: 1, 0xC0: 2, 0xE0: 3, 0xF0: 4}


def _utf8_lead_length(byte: int):
    """Length of the UTF-8 sequence whose lead byte this is, or None if the
    byte is a continuation byte (0x80..0xBF)."""
    if byte < 0x80:
        return 1
    if 0xC2 <= byte <= 0xDF:
        return 2
    if 0xE0 <= byte <= 0xEF:
        return 3
    if 0xF0 <= byte <= 0xF4:
        return 4
    return None


def split_incomplete_utf8_tail(data: bytes):
    """Return (decodable_prefix, possibly_incomplete_tail).

    A stream that stops in the middle of a multi-byte character is normal for
    UART logs: the ring buffer can also cut a character in half at its oldest
    edge. This lets ``tail_text`` hide a truncated character at the NEW edge
    (it may still arrive) while leaving a truncated character at the OLD edge
    to decode as U+FFFD (it is genuinely gone).
    """
    if not data:
        return data, b""
    for back in range(1, 5):
        if back > len(data):
            break
        length = _utf8_lead_length(data[-back])
        if length is None:
            continue  # continuation byte; keep walking back to the lead
        if length > back:
            return data[:-back], data[-back:]
        return data, b""
    return data, b""  # no lead byte within 4 bytes: old garbage, decode as-is


class RingBuffer:
    """Fixed-capacity byte buffer that discards oldest bytes on overflow."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self._buf = bytearray()
        self._total = 0
        self._lock = threading.Lock()

    def append(self, data: bytes) -> int:
        if not data:
            return 0
        with self._lock:
            self._buf.extend(data)
            overflow = len(self._buf) - self.capacity
            if overflow > 0:
                del self._buf[:overflow]
            self._total += len(data)
            return len(data)

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def clear(self) -> None:
        with self._lock:
            self._buf = bytearray()

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


class FileChardevSource:
    """Follow a ``-serial file:PATH`` chardev from outside QEMU's process."""

    name = "file"

    def __init__(self, path, *, connect_timeout: float = 30.0,
                 poll_interval: float = _POLL, idle_timeout: float = 0.5):
        self.path = str(path)
        self.connect_timeout = connect_timeout
        self.poll_interval = poll_interval
        self.idle_timeout = idle_timeout
        self._fh = None
        self._pos = 0

    def open(self) -> None:
        deadline = time.monotonic() + self.connect_timeout
        while True:
            try:
                self._fh = open(self.path, "rb")
                break
            except FileNotFoundError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"chardev file never appeared: {self.path}")
                time.sleep(self.poll_interval)
        self._pos = 0  # capture everything QEMU already wrote

    def read(self, size: int = CHUNK) -> bytes:
        if self._fh is None:
            self.open()
        deadline = time.monotonic() + self.idle_timeout
        while True:
            self._fh.seek(self._pos)
            data = self._fh.read(size)
            end = self._fh.tell()
            self._pos = end
            size_now = os.path.getsize(self.path)
            if size_now < end:  # QEMU restarted and truncated the file
                self._pos = 0
                continue
            if data:
                return data
            if size_now > end:
                continue  # more bytes pending right now
            if time.monotonic() >= deadline:
                return b""  # quiet guest; the caller keeps polling
            time.sleep(self.poll_interval)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


class UnixSocketChardevSource:
    """Connect to a ``-serial unix:PATH,server=on,wait=off`` chardev.

    QEMU is the listening server; this is the client, matching how ``nc -U``
    or a telnet client attaches to it. After the first successful connection a
    dropped stream (QEMU quit, console restart) is retried for
    ``reconnect_grace`` seconds; past that the stream is considered ENDED and
    reads return b"" — this is how a guest that SIGSEGVs QEMU terminates the
    console instead of spinning forever.
    """

    name = "unix"

    def __init__(self, path, *, connect_timeout: float = 30.0,
                 reconnect_grace: float = 3.0, poll_interval: float = _POLL):
        self.path = str(path)
        self.connect_timeout = connect_timeout
        self.reconnect_grace = reconnect_grace
        self.poll_interval = poll_interval
        self.connected_once = False
        self.ended = False
        self._sock = None

    def _try_connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        sock.setblocking(False)
        return sock

    def open(self) -> None:
        deadline = time.monotonic() + self.connect_timeout
        while True:
            try:
                self._sock = self._try_connect()
                self.connected_once = True
                return
            except (FileNotFoundError, socket.timeout):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"chardev socket not connectable: {self.path}")
                time.sleep(self.poll_interval)
            except ConnectionRefusedError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"chardev socket refused: {self.path}")
                time.sleep(self.poll_interval)

    def _retry_after_drop(self):
        """Reconnect after a drop; grace=0 means a drop ends the stream."""
        self._close_sock()
        if self.reconnect_grace <= 0:
            self.ended = True
            return
        deadline = time.monotonic() + self.reconnect_grace
        while time.monotonic() < deadline:
            try:
                self._sock = self._try_connect()
                return
            except OSError:
                time.sleep(self.poll_interval)
        self.ended = True

    def read(self, size: int = CHUNK) -> bytes:
        if self.ended:
            return b""
        if self._sock is None:
            if self.connected_once:
                self._retry_after_drop()
            else:
                self.open()
            if self._sock is None:
                return b""
        try:
            data = self._sock.recv(size)
        except BlockingIOError:
            time.sleep(self.poll_interval)
            return b""
        except (ConnectionResetError, BrokenPipeError):
            self._retry_after_drop()
            return b""
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                time.sleep(self.poll_interval)
                return b""
            self._retry_after_drop()
            return b""
        if not data:  # EOF: QEMU closed the chardev (usually quit)
            self._retry_after_drop()
            return b""
        return data

    def _close_sock(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        self._close_sock()
        self.ended = True


class GuestConsole:
    """Ring-buffered, thread-fed tail of one QEMU serial chardev.

    Memory is bounded by ``capacity`` (default 64 KiB); a guest stuck in a
    print loop cannot exhaust the bridge. Reads are cheap snapshots, safe from
    any thread (the HTTP handler), while the daemon reader thread appends.
    """

    def __init__(self, source, *, capacity: int = DEFAULT_CAPACITY):
        self.source = source
        self.ring = RingBuffer(capacity)
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> "GuestConsole":
        if self._thread is not None:
            raise RuntimeError("console already started")
        self._thread = threading.Thread(target=self._pump,
                                        name="guest-console-reader",
                                        daemon=True)
        self._thread.start()
        return self

    def _pump(self) -> None:
        try:
            self.source.open()
        except Exception as exc:
            if not self._stop.is_set():
                self.error = exc
            return
        while not self._stop.is_set():
            try:
                data = self.source.read(CHUNK)
            except Exception as exc:
                if not self._stop.is_set():
                    self.error = exc
                return
            if data:
                self.ring.append(data)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        try:
            self.source.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def bytes_in_ring(self) -> int:
        return len(self.ring)

    @property
    def total_bytes(self) -> int:
        return self.ring.total_bytes

    def tail(self, max_bytes=None) -> bytes:
        data = self.ring.snapshot()
        if max_bytes is not None:
            if max_bytes <= 0:
                raise ValueError("max_bytes must be positive")
            data = data[-max_bytes:]
        return data

    def tail_text(self, max_bytes=None) -> str:
        """Decode the tail as UTF-8. A character cut in half by the stream
        stopping mid-sequence is dropped (not replaced by U+FFFD); bytes the
        ring already evicted mid-character decode as U+FFFD."""
        prefix, partial = split_incomplete_utf8_tail(self.tail(max_bytes))
        return prefix.decode("utf-8", errors="replace")

    def wait_for(self, needle, timeout: float = 10.0,
                 poll_interval: float = _POLL) -> bool:
        """Block until the text is in the buffer (or raise ValueError)."""
        if isinstance(needle, str):
            needle = needle.encode("utf-8")
        deadline = time.monotonic() + timeout
        while True:
            if needle in self.ring.snapshot():
                return True
            if time.monotonic() >= deadline or self.error is not None:
                return False
            time.sleep(poll_interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    backend = parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--socket", metavar="PATH",
                         help="unix chardev path (server=on,wait=off)")
    backend.add_argument("--file", metavar="PATH",
                         help="file chardev path (-serial file:PATH)")
    parser.add_argument("--duration", type=float, default=30.0,
                        help="max seconds to listen (default 30)")
    parser.add_argument("--until", metavar="TEXT",
                        help="exit once this text appears in the output")
    parser.add_argument("--max-wait", type=float, default=120.0,
                        help="deadline for --until, on top of --duration")
    parser.add_argument("--capacity", type=int, default=DEFAULT_CAPACITY)
    parser.add_argument("--tail-bytes", type=int, default=None,
                        help="print only the last N bytes")
    args = parser.parse_args(argv)

    if args.socket:
        source = UnixSocketChardevSource(args.socket)
    else:
        source = FileChardevSource(args.file)
    console = GuestConsole(source, capacity=args.capacity)
    try:
        source.open()
    except TimeoutError as exc:
        print(f"guest-console: {exc}", file=sys.stderr)
        return 2

    def deadline_for():
        if args.until:
            return time.monotonic() + args.max_wait
        return time.monotonic() + args.duration

    deadline = deadline_for()
    while time.monotonic() < deadline:
        try:
            data = source.read(CHUNK)
        except TimeoutError as exc:
            print(f"guest-console: {exc}", file=sys.stderr)
            break
        if data:
            console.ring.append(data)
            if args.until and args.until.encode("utf-8") in console.tail():
                break
        if source.name == "unix" and getattr(source, "ended", False):
            print("guest-console: stream ended (QEMU closed the chardev)",
                  file=sys.stderr)
            break
    text = console.tail_text(args.tail_bytes)
    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
