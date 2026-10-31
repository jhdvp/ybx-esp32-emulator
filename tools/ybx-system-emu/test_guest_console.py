"""Host tests for guest_console: ring buffer, decoding, sources, CLI.

No QEMU, no network: the socket tests use an AF_UNIX listener inside a
tempdir, which is exactly what a ``-serial unix:...,server=on`` chardev looks
like from the client side. Every test is time-boxed with small poll intervals
so the whole file runs in well under a second.
"""

import os
import socket
import tempfile
import threading
import time
import unittest

import guest_console as gc


class RingBufferTests(unittest.TestCase):
    def test_empty(self):
        ring = gc.RingBuffer(16)
        self.assertEqual(ring.snapshot(), b"")
        self.assertEqual(len(ring), 0)
        self.assertEqual(ring.total_bytes, 0)

    def test_append_below_capacity(self):
        ring = gc.RingBuffer(16)
        ring.append(b"hello")
        self.assertEqual(ring.snapshot(), b"hello")
        self.assertEqual(ring.total_bytes, 5)

    def test_wraparound_keeps_newest_bytes(self):
        ring = gc.RingBuffer(6)
        ring.append(b"abcd")
        ring.append(b"efgh")
        self.assertEqual(ring.snapshot(), b"cdefgh")
        self.assertEqual(len(ring), 6)
        self.assertEqual(ring.total_bytes, 8)

    def test_single_write_larger_than_capacity_truncates_to_tail(self):
        ring = gc.RingBuffer(5)
        ring.append(b"0123456789")
        self.assertEqual(ring.snapshot(), b"56789")
        self.assertEqual(len(ring), 5)
        self.assertEqual(ring.total_bytes, 10)

    def test_capacity_is_a_hard_bound_under_many_writes(self):
        ring = gc.RingBuffer(1024)
        chunk = b"x" * 300
        for _ in range(100):
            ring.append(chunk)
        self.assertEqual(len(ring), 1024)
        self.assertEqual(ring.total_bytes, 30000)

    def test_exact_capacity_boundary(self):
        ring = gc.RingBuffer(4)
        ring.append(b"abcd")
        self.assertEqual(ring.snapshot(), b"abcd")
        ring.append(b"e")
        self.assertEqual(ring.snapshot(), b"bcde")

    def test_rejects_bad_capacity(self):
        with self.assertRaises(ValueError):
            gc.RingBuffer(0)
        with self.assertRaises(ValueError):
            gc.RingBuffer(-1)

    def test_clear(self):
        ring = gc.RingBuffer(16)
        ring.append(b"abc")
        ring.clear()
        self.assertEqual(ring.snapshot(), b"")
        self.assertEqual(ring.total_bytes, 3)


class Utf8TailTests(unittest.TestCase):
    def test_complete_multibyte_survives(self):
        data = "Привет мир".encode("utf-8")
        prefix, partial = gc.split_incomplete_utf8_tail(data)
        self.assertEqual(partial, b"")
        self.assertEqual(prefix.decode("utf-8"), "Привет мир")

    def test_partial_tail_is_split_off(self):
        full = "boot €".encode("utf-8")  # euro sign is 3 bytes
        for cut in (1, 2):  # stream stops mid-character
            prefix, partial = gc.split_incomplete_utf8_tail(full[:len(full) - cut])
            self.assertEqual(prefix + partial, full[:len(full) - cut])
            self.assertEqual(prefix.decode("utf-8"), "boot ")
            self.assertTrue(partial, "truncated sequence must be reported")
        # cutting the whole character off leaves nothing partial
        prefix, partial = gc.split_incomplete_utf8_tail(full[:5])
        self.assertEqual(partial, b"")
        self.assertEqual(prefix, full[:5])

    def test_4byte_sequence_partial(self):
        data = "😀".encode("utf-8")  # F0 9F 98 80
        prefix, partial = gc.split_incomplete_utf8_tail(data[:2])
        self.assertEqual(prefix, b"")
        self.assertEqual(partial, data[:2])

    def test_ascii_tail_is_complete(self):
        prefix, partial = gc.split_incomplete_utf8_tail(b"boot\r\n")
        self.assertEqual(partial, b"")
        self.assertEqual(prefix, b"boot\r\n")

    def test_empty_input(self):
        self.assertEqual(gc.split_incomplete_utf8_tail(b""), (b"", b""))

    def test_all_continuation_bytes_not_treated_as_partial(self):
        # more than 3 continuation bytes in a row: no valid lead, old garbage
        data = b"\x80\x80\x80\x80"
        prefix, partial = gc.split_incomplete_utf8_tail(data)
        self.assertEqual(partial, b"")
        self.assertEqual(prefix, data)


class _StubSource:
    """Yields queued chunks, then simulates a quiet guest."""

    name = "stub"

    def __init__(self, chunks, *, raise_on_open=None, raise_on_read=None):
        self._chunks = list(chunks)
        self.raise_on_open = raise_on_open
        self.raise_on_read = raise_on_read
        self.closed = False

    def open(self):
        if self.raise_on_open:
            raise self.raise_on_open

    def read(self, size=4096):
        if self._chunks:
            return self._chunks.pop(0)
        if self.raise_on_read:
            raise self.raise_on_read
        time.sleep(0.01)
        return b""

    def close(self):
        self.closed = True


class GuestConsoleThreadTests(unittest.TestCase):
    def test_empty_stream_yields_empty_tail(self):
        console = gc.GuestConsole(_StubSource([]), capacity=64).start()
        try:
            time.sleep(0.05)
            self.assertEqual(console.tail(), b"")
            self.assertEqual(console.tail_text(), "")
            self.assertIsNone(console.error)
            self.assertTrue(console.running)
        finally:
            console.stop()
        self.assertFalse(console.running)

    def test_reader_thread_fills_ring(self):
        console = gc.GuestConsole(
            _StubSource([b"ESP-ROM:esp32s3\r\n", b"entry 0x403c8898"]),
            capacity=256).start()
        try:
            self.assertTrue(console.wait_for("entry 0x", timeout=2.0))
            self.assertEqual(
                console.tail_text(),
                "ESP-ROM:esp32s3\r\nentry 0x403c8898")
        finally:
            console.stop()

    def test_thread_wraps_ring_at_capacity(self):
        console = gc.GuestConsole(
            _StubSource([b"A" * 100, b"B" * 100, b"C" * 100]),
            capacity=150).start()
        try:
            self.assertTrue(console.wait_for(b"CCC", timeout=2.0))
            deadline = time.monotonic() + 2.0
            while console.total_bytes < 300 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(console.total_bytes, 300)
            self.assertEqual(console.bytes_in_ring, 150)
            tail = console.tail()
            self.assertEqual(len(tail), 150)
            self.assertTrue(tail.endswith(b"C" * 100))
            self.assertNotIn(b"A", tail)  # oldest chunk fully evicted
        finally:
            console.stop()

    def test_open_error_is_recorded_not_raised(self):
        console = gc.GuestConsole(
            _StubSource([], raise_on_open=TimeoutError("no chardev")),
            capacity=64).start()
        try:
            deadline = time.monotonic() + 2.0
            while console.error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsInstance(console.error, TimeoutError)
            self.assertEqual(console.tail(), b"")
        finally:
            console.stop()

    def test_read_error_is_recorded(self):
        console = gc.GuestConsole(
            _StubSource([b"ok"], raise_on_read=OSError("boom")),
            capacity=64).start()
        try:
            deadline = time.monotonic() + 2.0
            while console.error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsInstance(console.error, OSError)
            self.assertEqual(console.tail(), b"ok")
        finally:
            console.stop()

    def test_tail_max_bytes_and_validation(self):
        console = gc.GuestConsole(_StubSource([b"0123456789"]), capacity=64)
        console.start()
        try:
            console.wait_for(b"9", timeout=2.0)
            self.assertEqual(console.tail(4), b"6789")
            self.assertEqual(console.tail_text(4), "6789")
            with self.assertRaises(ValueError):
                console.tail(0)
        finally:
            console.stop()

    def test_tail_text_drops_half_character_at_stream_end(self):
        source = _StubSource(["Привет".encode("utf-8")[:len("Привет".encode()) - 1]])
        console = gc.GuestConsole(source, capacity=64).start()
        try:
            console.wait_for("Приве", timeout=2.0)
            text = console.tail_text()
            self.assertEqual(text, "Приве")
            self.assertNotIn("\ufffd", text)
        finally:
            console.stop()

    def test_tail_text_replaces_bytes_lost_to_wrap(self):
        # ring edge cuts the 3-byte euro sign in half: honest U+FFFD
        data = ("boot " + "€" + "tail").encode("utf-8")  # 12 bytes
        console = gc.GuestConsole(_StubSource([data]), capacity=6).start()
        try:
            console.wait_for(b"tail", timeout=2.0)
            text = console.tail_text()
            self.assertTrue(text.endswith("tail"))
            self.assertIn("\ufffd", text)  # evicted half-character is gone
        finally:
            console.stop()

    def test_double_start_rejected(self):
        console = gc.GuestConsole(_StubSource([])).start()
        try:
            with self.assertRaises(RuntimeError):
                console.start()
        finally:
            console.stop()

    def test_wait_for_timeout_and_stop(self):
        console = gc.GuestConsole(_StubSource([b"hello"])).start()
        try:
            self.assertFalse(console.wait_for("goodbye", timeout=0.2))
        finally:
            console.stop()
        console2 = gc.GuestConsole(_StubSource([b"x"]))
        console2.stop()  # stop before start must not raise
        self.assertFalse(console2.running)


class FileSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "guest0.log")

    def test_follows_a_growing_file(self):
        open(self.path, "wb").close()
        console = gc.GuestConsole(
            gc.FileChardevSource(self.path, poll_interval=0.01,
                                 idle_timeout=0.05),
            capacity=1024).start()
        try:
            with open(self.path, "ab") as fh:
                fh.write(b" Adding SPI flash device\n")
                fh.flush()
                self.assertTrue(console.wait_for("SPI flash", timeout=2.0))
                time.sleep(0.1)
                with open(self.path, "ab") as fh:
                    fh.write(b"ESP-ROM:esp32s3\n")
                    fh.flush()
                self.assertTrue(console.wait_for("ESP-ROM", timeout=2.0))
                self.assertIn("Adding SPI flash device", console.tail_text())
                self.assertTrue(console.tail_text().endswith("ESP-ROM:esp32s3\n"))
            # console is read-only: it must never touch the file's content
            with open(self.path, "rb") as check:
                self.assertEqual(
                    check.read(),
                    b" Adding SPI flash device\nESP-ROM:esp32s3\n")
        finally:
            console.stop()

    def test_detects_truncation_restart(self):
        with open(self.path, "wb") as fh:
            fh.write(b"old run output")
        source = gc.FileChardevSource(self.path, poll_interval=0.01,
                                      idle_timeout=0.05)
        console = gc.GuestConsole(source, capacity=1024).start()
        try:
            self.assertTrue(console.wait_for("old run", timeout=2.0))
            with open(self.path, "wb") as fh:  # QEMU restart truncates
                fh.write(b"new run")
            self.assertTrue(console.wait_for("new run", timeout=2.0))
        finally:
            console.stop()

    def test_missing_file_times_out(self):
        source = gc.FileChardevSource(os.path.join(self.tmp.name, "nope"),
                                      connect_timeout=0.2,
                                      poll_interval=0.01)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            source.open()
        self.assertLess(time.monotonic() - start, 2.0)


class _SocketServer:
    """Stands in for QEMU's `server=on,wait=off` unix chardev."""

    def __init__(self, path):
        self.path = path
        self.conn = None
        try:  # a closed listener leaves the path behind; fresh bind needs it
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(path)
        self._listener.listen(1)

    def accept(self, timeout=2.0):
        self._listener.settimeout(timeout)
        self.conn = self._listener.accept()[0]
        return self.conn

    def send(self, data):
        self.conn.sendall(data)

    def close(self):
        for sock in (self.conn, self._listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


class SocketSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gc-sock-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "guest0.sock")

    def test_connects_and_reads(self):
        server = _SocketServer(self.path)
        self.addCleanup(server.close)
        console = gc.GuestConsole(
            gc.UnixSocketChardevSource(self.path, poll_interval=0.01),
            capacity=1024).start()
        try:
            server.accept()
            server.send(b"rst:0x1 (POWERON),boot:0x4 (SPI_FLASH_BOOT)\r\n")
            self.assertTrue(console.wait_for("POWERON", timeout=2.0))
            self.assertTrue(source_ended_ok(console))
        finally:
            console.stop()

    def test_retries_until_socket_appears(self):
        # QEMU is slow to create the chardev socket; the console waits
        console = gc.GuestConsole(
            gc.UnixSocketChardevSource(self.path, connect_timeout=2.0,
                                       poll_interval=0.01),
            capacity=1024).start()
        server = None
        try:
            def late_open():
                nonlocal server
                time.sleep(0.2)
                server = _SocketServer(self.path)
                server.accept()
                server.send(b"late boot log")
            threading.Thread(target=late_open, daemon=True).start()
            self.assertTrue(console.wait_for("late boot", timeout=3.0))
        finally:
            console.stop()
            if server:
                server.close()

    def test_eof_ends_stream_after_grace_expires(self):
        server = _SocketServer(self.path)
        source = gc.UnixSocketChardevSource(self.path, poll_interval=0.01,
                                            reconnect_grace=0.1)
        console = gc.GuestConsole(source, capacity=1024).start()
        try:
            server.accept()
            server.send(b"first bytes")
            self.assertTrue(console.wait_for("first bytes", timeout=2.0))
            server.conn.close()
            server.close()  # QEMU fully gone: EOF, nobody to reconnect to
            deadline = time.monotonic() + 2.0
            while not source.ended and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(source.ended)
            self.assertEqual(console.tail(), b"first bytes")
            self.assertIsNone(console.error)  # EOF is a normal end, not an error
        finally:
            console.stop()
            server.close()

    def test_reconnect_after_drop_keeps_ring(self):
        server = _SocketServer(self.path)
        source = gc.UnixSocketChardevSource(self.path, poll_interval=0.01,
                                            reconnect_grace=2.0)
        console = gc.GuestConsole(source, capacity=1024).start()

        def drop_and_relisten():
            server.accept()
            server.send(b"chunk one")
            server.conn.close()
            server.close()
            time.sleep(0.05)
            # a fresh QEMU chardev on the same path
            server2 = _SocketServer(source_path)
            server2.accept()
            server2.send(b"chunk two")
            reconnect_threads.append(server2)
        source_path = self.path
        reconnect_threads = []
        try:
            threading.Thread(target=drop_and_relisten, daemon=True).start()
            self.assertTrue(console.wait_for("chunk two", timeout=3.0))
            self.assertIn(b"chunk one", console.tail())
            self.assertIsNone(console.error)
        finally:
            console.stop()
            server.close()
            for s in reconnect_threads:
                s.close()

    def test_missing_socket_times_out(self):
        source = gc.UnixSocketChardevSource(
            os.path.join(self.tmp.name, "nope.sock"),
            connect_timeout=0.2, poll_interval=0.01)
        with self.assertRaises(TimeoutError):
            source.open()

    def test_grace_zero_ends_on_first_drop(self):
        server = _SocketServer(self.path)
        source = gc.UnixSocketChardevSource(self.path, poll_interval=0.01,
                                            reconnect_grace=0.0)
        console = gc.GuestConsole(source, capacity=1024).start()
        try:
            server.accept()
            server.send(b"bye")
            self.assertTrue(console.wait_for("bye", timeout=2.0))
            server.conn.close()
            deadline = time.monotonic() + 2.0
            while not source.ended and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(source.ended)
        finally:
            console.stop()
            server.close()


def source_ended_ok(console) -> bool:
    return console.error is None


class CliTests(unittest.TestCase):
    def test_file_backend_prints_tail_and_until_exits(self):
        tmp = tempfile.TemporaryDirectory(prefix="gc-cli-")
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "uart.log")
        with open(path, "wb") as fh:
            fh.write(b"ESP-ROM:esp32s3-20210327\nentry 0x403c8898\n")
        code = _run_cli_capture(["--file", path, "--until", "entry 0x",
                                 "--max-wait", "5"])
        self.assertEqual(code, 0)
        self.assertIn("ESP-ROM:esp32s3-20210327", _run_cli_capture.__last_output__)

    def test_missing_chardev_exits_2(self):
        code = _run_cli_capture(["--socket", "/nonexistent/guest.sock",
                                 "--duration", "0.05"],
                                connect_timeout_patch=0.2)
        self.assertEqual(code, 2)


def _run_cli_capture(argv, *, connect_timeout_patch=None):
    import io
    import contextlib
    real_main = gc.main
    if connect_timeout_patch is not None:
        orig = gc.UnixSocketChardevSource

        class Patched(orig):
            def __init__(self, path, **kw):
                kw["connect_timeout"] = connect_timeout_patch
                super().__init__(path, **kw)
        gc.UnixSocketChardevSource = Patched
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gc.main(argv)
    finally:
        if connect_timeout_patch is not None:
            gc.UnixSocketChardevSource = orig  # noqa: F821
    _run_cli_capture.__last_output__ = out.getvalue()
    return code


if __name__ == "__main__":
    unittest.main()
