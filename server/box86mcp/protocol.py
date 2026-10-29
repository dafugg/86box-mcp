"""Client for the 86Box automation socket (one text line out, one JSON line back)."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import dataclass


class AutomationError(RuntimeError):
    pass


# Automation socket protocol versions this client understands.
SUPPORTED_PROTOCOLS = {1}


CP437 = (
    "\x00☺☻♥♦♣♠•◘○◙♂♀♪♫☼►◄↕‼¶§▬↨↑↓→←∟↔▲▼"
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~⌂"
    "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀"
    "αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■\xa0"
)


def decode_cp437(data: bytes) -> str:
    return "".join(" " if b == 0 else CP437[b] for b in data)


@dataclass
class TextScreen:
    mode: int
    cols: int
    rows: int
    cursor: tuple[int, int]
    lines: list[str]
    attrs: list[bytes]

    def render(self, cursor: bool = True) -> str:
        out = [line.rstrip() for line in self.lines]
        while out and not out[-1]:
            out.pop()
        text = "\n".join(out)
        if cursor:
            text += f"\n[mode {self.mode:02x}h {self.cols}x{self.rows}, cursor row {self.cursor[0]} col {self.cursor[1]}]"
        return text


class Automation:
    """Synchronous client. Each call blocks until 86Box has executed the command.

    endpoint is "unix:<path>" (or a bare path) or "tcp:<host>:<port>". TCP needs the token 86Box
    wrote to token_file; it is sent once, right after connecting.
    """

    def __init__(self, endpoint: str, token_file: str | None = None, timeout: float = 600.0):
        self.endpoint = endpoint
        self.token_file = token_file
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buf = b""
        # Called with (request, reply) when a timed command ends early because 86Box's GDB stub has
        # stopped the CPU; returns the reply to use.
        self.on_debugger_stop = None
        # One request at a time, from any thread (a sound puller runs alongside the tools).
        self._lock = threading.RLock()
        # Called with each reply that carries the emulated time (not from inside itself), and the
        # longest stretch of emulated time one step or wait may take: while sound is being pulled,
        # longer ones are split so that no more than this passes between pulls.
        self.after_reply = None
        self.chunk_ms: int | None = None
        self._in_hook = False

    @property
    def path(self) -> str:
        return self.endpoint[5:] if self.endpoint.startswith("unix:") else self.endpoint

    def _open(self) -> socket.socket:
        if self.endpoint.startswith("tcp:"):
            host, _, port = self.endpoint[4:].rpartition(":")
            return socket.create_connection((host, int(port)), timeout=self.timeout)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(self.path)
        except OSError:
            s.close()
            raise
        return s

    def connect(self, wait: float = 30.0) -> None:
        deadline = time.monotonic() + wait
        while True:
            try:
                s = self._open()
                s.settimeout(self.timeout)
                self.sock = s
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if time.monotonic() > deadline:
                    raise AutomationError(f"86Box automation endpoint {self.endpoint} did not come up")
                time.sleep(0.1)
        if self.endpoint.startswith("tcp:") and self.token_file:
            # Without a token (86Box run with --automation-insecure) there's nothing to send; an 86Box
            # that does want one answers the first request with "authentication required".
            with open(self.token_file) as f:
                token = f.read().strip()
            self.call(f"auth {token}")

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None
        self._buf = b""

    def call(self, line: str) -> dict:
        with self._lock:
            reply = self._call(line)
            if self.after_reply and not self._in_hook and "ms" in reply:
                self._in_hook = True
                try:
                    self.after_reply(reply)
                finally:
                    self._in_hook = False
            return reply

    def _call(self, line: str) -> dict:
        if "\n" in line:
            raise ValueError("request must be a single line")
        if not self.sock:
            self.connect()
        assert self.sock
        self.sock.sendall(line.encode("latin-1") + b"\n")
        reply = self._reply()
        if reply.get("stopped") == "debugger" and self.on_debugger_stop:
            return self.on_debugger_stop(line, reply)
        return reply

    def _reply(self) -> dict:
        assert self.sock
        while b"\n" not in self._buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                self.close()
                raise AutomationError("86Box closed the automation socket")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\n", 1)
        reply = json.loads(raw.decode("utf-8", "replace"))     # the log can hold any bytes
        if reply.get("fatal"):
            self.close()                                       # 86Box closes it after this line
            raise AutomationError(f"86Box stopped with a fatal error: {reply.get('error')}")
        if not reply.get("ok"):
            raise AutomationError(reply.get("error", "unknown error"))
        return reply

    def put_file(self, name: str, path: str | os.PathLike) -> dict:
        """Send a file to 86Box (media put), for an 86Box on another machine; it lands in the VM
        directory's automation-media folder, and the reply's "path" is where 86Box can open it."""
        size = os.path.getsize(path)
        with self._lock:
            if not self.sock:
                self.connect()
            assert self.sock
            try:
                with open(path, "rb") as f:
                    self.sock.sendall(f"media put {name} {size}\n".encode())
                    left = size
                    while left and (chunk := f.read(min(left, 1 << 20))):
                        self.sock.sendall(chunk)
                        left -= len(chunk)
                    if left:            # it shrank on the way: 86Box would wait for the rest forever
                        raise OSError(f"{path} changed while it was being sent")
            except OSError:
                self.close()        # part of a file in flight: the connection can't be used again
                raise
            return self._reply()

    def can_put_files(self) -> bool:
        """Whether this 86Box takes files over the connection (media put): asking it to remove a file
        by a name it never accepts tells an 86Box that has the command from one that hasn't."""
        try:
            self.call("media remove .")
        except AutomationError as e:
            return "bad name" in str(e)
        return True

    def log(self, start: int = 0) -> dict:
        """What 86Box has logged from position `start` on (its last 64 KB), with "text" and "next"."""
        return self.call(f"log {start}")

    # Convenience wrappers ------------------------------------------------

    def status(self) -> dict:
        return self.call("status")

    def info(self) -> dict:
        r = self.call("info")
        r.pop("ok", None)
        return r

    def hold(self) -> dict:
        return self.call("hold")

    def release(self) -> dict:
        return self.call("release")

    def fast_forward(self, on: bool | None = None) -> dict:
        """Turn fast forward on or off (None: just report it). The reply's ff is the state now."""
        return self.call("ff" if on is None else f"ff {'on' if on else 'off'}")

    def check_quit_soft(self) -> None:
        """Raise unless this 86Box understands quit soft."""
        # Before 86Box#8382, any "quit ..." was a hard quit. #8384 (ff, which status reports) was
        # merged after it, so a status with ff means quit soft is understood.
        if "ff" not in self.status():
            raise AutomationError("this 86Box predates 'quit soft' (86Box#8382); it would power off hard")

    def quit_soft(self) -> dict:
        """Press the machine's power button: ACPI, or the board's own, else a plain power-off."""
        self.check_quit_soft()
        return self.call("quit soft")

    def _chunked(self, timeout_ms: int, request, done=lambda r: bool(r.get("matched"))) -> dict:
        """A timed command in pieces of at most chunk_ms (when set): request(ms) -> reply."""
        timeout_ms = int(timeout_ms)
        if not self.chunk_ms or timeout_ms <= self.chunk_ms:
            return request(timeout_ms)
        left = timeout_ms
        while True:
            q = min(left, self.chunk_ms)
            r = request(q)
            left -= q
            if left <= 0 or done(r) or r.get("stopped"):
                return r

    def step(self, ms: int) -> dict:
        return self._chunked(ms, lambda q: self.call(f"step {q}"), done=lambda r: False)

    def keys(self, down: bool, scancodes: list[int]) -> dict:
        if not scancodes:
            return self.status()
        codes = " ".join(f"0x{c:x}" for c in scancodes)
        return self.call(f"key {'down' if down else 'up'} {codes}")

    def mouse(self, dx: int, dy: int, buttons: int) -> dict:
        return self.call(f"mouse {int(dx)} {int(dy)} {int(buttons)}")

    def screenshot(self, path: str | None = None) -> dict:
        """With a path, 86Box writes the PNG there (on its own machine). Without, the PNG comes back
        in the reply as bytes under "png", which also works across a network."""
        if path:
            return self.call(f"screenshot {path}")
        import base64

        r = self.call("screenshot")
        r["png"] = base64.b64decode(r["png"])
        return r

    def text(self) -> TextScreen:
        r = self.call("text")
        return TextScreen(
            mode=r["mode"],
            cols=r["cols"],
            rows=r["rows"],
            cursor=tuple(r["cursor"]),
            lines=[decode_cp437(bytes.fromhex(h)) for h in r["chars"]],
            attrs=[bytes.fromhex(h) for h in r["attrs"]],
        )

    @staticmethod
    def _mem_opts(view: str | None, width: int) -> str:
        # The plain form for the defaults, which every 86Box with the socket understands.
        return (f" {view}" if view else "") + (f" {int(width)}" if width != 1 else "")

    def memr(self, addr: int, length: int, view: str | None = None, width: int = 1) -> bytes:
        """view: bus (the default), cpu, normal, smm or linear; width: the access size, 1, 2 or 4."""
        return bytes.fromhex(self.call(f"memr 0x{addr:x} {int(length)}{self._mem_opts(view, width)}")["data"])

    def memw(self, addr: int, data: bytes, view: str | None = None, width: int = 1) -> dict:
        return self.call(f"memw 0x{addr:x} {data.hex()}{self._mem_opts(view, width)}")

    def ior(self, port: int, width: int = 1, count: int = 1) -> list[int]:
        return self.call(f"ior 0x{port:x} {int(width)} {int(count)}")["values"]

    def iow(self, port: int, width: int, values: list[int]) -> dict:
        return self.call(f"iow 0x{port:x} {int(width)} " + " ".join(f"0x{v:x}" for v in values))

    def pcir(self, bus: int, dev: int, fn: int, reg: int, length: int, width: int = 4) -> tuple[bool, bytes]:
        """(whether a card answers at bus:dev, the bytes) of configuration space, in width-byte cycles."""
        r = self.call(f"pcir {bus} {dev} {fn} 0x{reg:x} {int(length)} {int(width)}")
        return r["present"], bytes.fromhex(r["data"])

    def pciw(self, bus: int, dev: int, fn: int, reg: int, data: bytes, width: int = 1) -> bool:
        return self.call(f"pciw {bus} {dev} {fn} 0x{reg:x} {data.hex()} {int(width)}")["present"]

    def regs(self) -> dict:
        r = self.call("regs")
        r.pop("ok", None)
        return r

    def audio_level(self, ms: int) -> dict:
        return self.call(f"audio level {int(ms)}")["streams"]

    def audio_read(self, stream: str, ms: int) -> dict:
        """{"rate", "frames", "end_ms", "pcm": bytes of 16-bit stereo}"""
        import base64

        r = self.call(f"audio read {stream} {int(ms)}")
        r["pcm"] = base64.b64decode(r.pop("data"))
        return r

    def wait_sound(self, timeout_ms: int, db: float) -> dict:
        return self._chunked(timeout_ms, lambda q: self.call(f"wait sound {q} {float(db):g}"))

    def wait_silence(self, timeout_ms: int, db: float, for_ms: int, quiet_since_ms: int | None = None) -> dict:
        since = [quiet_since_ms]

        def request(q):
            arg = "" if since[0] is None else f" {int(since[0])}"
            r = self.call(f"wait silence {q} {float(db):g} {int(for_ms)}{arg}")
            since[0] = r.get("quiet_since_ms", since[0])
            return r
        return self._chunked(timeout_ms, request)

    def fpu(self) -> dict:
        r = self.call("fpu")
        r.pop("ok", None)
        return r

    def wait_text(self, text: str, timeout_ms: int) -> dict:
        return self._chunked(timeout_ms, lambda q: self.call(f"wait text {q} {text}"))

    def wait_mem(self, addr: int, data: bytes, timeout_ms: int) -> dict:
        return self._chunked(timeout_ms, lambda q: self.call(f"wait mem {q} 0x{addr:x} {data.hex()}"))

    # Serial ports with the "automation" backend (serialN_device = automation) ----------------

    # Bytes per serial write, to stay well inside the socket's 4096-byte request line.
    SERIAL_WRITE_CHUNK = 1900

    def serial_out(self, port: int, from_pos: int = 0, max_bytes: int | None = None) -> dict:
        """What the guest sent on COM<port> from position from_pos on: the reply, with "data" as bytes.
        "next" is where to read from next; "lost" counts bytes dropped before they were read."""
        r = self.call(f"serial read {int(port)} {int(from_pos)}" + (f" {int(max_bytes)}" if max_bytes else ""))
        r["data"] = bytes.fromhex(r["data"])
        return r

    def serial_in(self, port: int, data: bytes) -> int:
        """Queue bytes for the guest on COM<port>; they arrive at its UART's rate. Returns how many were
        queued: fewer than given if the guest's input queue (64 KiB) filled up."""
        for i in range(0, len(data), self.SERIAL_WRITE_CHUNK):
            try:
                self.call(f"serial write {int(port)} {data[i:i + self.SERIAL_WRITE_CHUNK].hex()}")
            except AutomationError as e:
                if "input queue full" in str(e):
                    return i
                raise
        return len(data)

    def serial_lines(self, port: int, cts: bool, dsr: bool, dcd: bool, ri: bool = False) -> dict:
        """Set the modem status lines the guest sees on COM<port>; the reply has the guest's DTR/RTS."""
        return self.call(f"serial lines {int(port)} {int(cts)} {int(dsr)} {int(dcd)} {int(ri)}")

    def wait_serial(self, port: int, from_pos: int, text: bytes, timeout_ms: int) -> dict:
        return self._chunked(timeout_ms, lambda q: self.call(
            f"wait serial {q} {int(port)} {int(from_pos)} {text.decode('latin-1')}"))
