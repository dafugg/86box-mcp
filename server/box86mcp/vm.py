"""Launching 86Box headless and preparing media for it."""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import contextlib
import hashlib

from .gdbrsp import Gdb, describe_stop
from .guard import GRACE_S as GUARD_GRACE_S
from .protocol import SUPPORTED_PROTOCOLS, Automation, AutomationError


def _env_path(name: str, default: str) -> Path:
    return Path(os.path.expanduser(os.environ.get(name, default)))


_REPO = Path(__file__).resolve().parents[2]


def _default_binary() -> str:
    """A native build in 86box/build-mcp made with -D AUTOMATION=ON, else the Docker wrapper.
    Windows has no Docker wrapper: there it's the native build, or BOX86_BINARY."""
    build = _REPO / "86box" / "build-mcp"
    native = build / "src" / ("86Box.exe" if os.name == "nt" else "86Box")
    try:
        automation = "AUTOMATION:BOOL=ON" in (build / "CMakeCache.txt").read_text()
    except OSError:
        automation = False
    if os.name == "nt" or (native.exists() and automation):
        return str(native)
    return str(_REPO / "docker" / "86box.sh")


BINARY = _env_path("BOX86_BINARY", _default_binary())
# Insecure mode, for machines you trust: 86Box's TCP automation socket without its token (86Box
# --automation-insecure), and the MCP server's HTTP transport on a non-loopback address.
INSECURE = os.environ.get("BOX86_INSECURE") == "1"

# Disk images we build must not depend on when they were built, or two runs of the same script
# differ (in DIR listings, for a start): a fixed volume serial for mformat, and a fixed timestamp
# for files we generate. Host files keep their own timestamps (mcopy -m).
VOLUME_SERIAL = "86B0C0DE"
FIXED_MTIME = 1_700_000_000  # 2023-11-14
# FAT dates are local time; mtools converts in UTC so images don't depend on the host's zone.
MTOOLS_ENV = {**os.environ, "TZ": "UTC"}
# Without BOX86_ROMS, 86Box finds its ROMs itself: in the VM's roms folder, next to the binary, or in
# the standard data folders (~/.local/share/86Box/roms, /usr/share/86Box/roms on Linux).
ROMS = _env_path("BOX86_ROMS", "") if os.environ.get("BOX86_ROMS") else None
# mtools comes from PATH; BOX86_MTOOLS_DIR adds a folder to look in as well.
MTOOLS_DIR = os.path.expanduser(os.environ["BOX86_MTOOLS_DIR"]) if os.environ.get("BOX86_MTOOLS_DIR") else None
# A saved CMOS for the default machine, with the hard disk set up, so the BIOS boots without prompting.
PRIMED_NVR = Path(__file__).resolve().parent / "data" / "primed-nvr"


# A 486DX2-66 VLB board close to typical period hardware; time_sync disabled (86Box reads it from
# [Machine]) so the clock starts from the saved CMOS, not the host's clock, and runs are repeatable.
# Format with hdd_fn/cyls/heads/spt/speed.
DX2_66_CFG = """[General]
confirm_exit = 0
confirm_reset = 0
confirm_save = 0
vid_renderer = qt_software

[Machine]
time_sync = disabled
cpu_family = i486dx2
cpu_multi = 2
cpu_speed = 66666666
cpu_use_dynarec = 0
fpu_type = internal
machine = 486sp3c
mem_size = 16384

[Video]
gfxcard = cl_gd5428_migrated_vlb

[Input devices]
keyboard_type = keyboard_ps2
mouse_type = ps2

[Sound]
sndcard = sb16

[Hard disks]
hdd_01_fn = {hdd_fn}
hdd_01_ide_channel = 0:0
hdd_01_parameters = {spt}, {heads}, {cyls}, 0, ide
hdd_01_speed = {speed}

[Floppy and CD-ROM drives]
fdd_01_type = 35_2hd
cdrom_01_parameters = 1, atapi
cdrom_01_ide_channel = 0:1

[Cirrus Logic GD5428 (VLB)]
bios = cl_gd5428_vlb
"""


def copy_identity(lines: list[str], name: str) -> list[str]:
    """A copied VM's 86box.cfg lines without the original's uuid, and with MACs of its own.

    Each network card's MAC becomes one made from the old MAC and name (the new VM's folder name),
    so copies named differently differ, and a copy made again under the same name gets the same
    MAC. Removing the MAC instead makes 86Box draw a new one from the host's clock, and games that
    read it (Warcraft II's setup does, through IPX) then play differently from run to run.
    """
    out = []
    for l in lines:
        key, _, value = l.partition("=")
        if key.strip() == "uuid":
            continue
        if key.strip() == "mac":
            h = hashlib.sha256(f"{value.strip()}/{name}".encode()).digest()
            l = f"mac = {h[0]:02x}:{h[1]:02x}:{h[2]:02x}"
        out.append(l)
    return out


def write_config(vm_dir: str | os.PathLike, cyls: int, heads: int, spt: int, hdd_fn: str = "hdd.img",
                 speed: str = "ramdisk", template: str | os.PathLike | None = None) -> Path:
    """Write the VM's 86box.cfg: the DX2-66 machine with one IDE disk, or a copy of template.

    With a template, everything is kept except the first hard disk, which becomes hdd_fn with the
    given geometry (on the template's bus and channel if it has one, else IDE 0:0).
    """
    cfg = Path(vm_dir) / "86box.cfg"
    if template is None:
        cfg.write_text(DX2_66_CFG.format(hdd_fn=hdd_fn, cyls=cyls, heads=heads, spt=spt, speed=speed))
        return cfg
    lines = Path(template).expanduser().read_text().splitlines()
    # The template's identity must not come along: with a network card configured, 86Box sees a
    # known uuid in a new folder as a moved or copied machine and stops at a dialog asking which
    # (invisible when headless). Without a uuid it just makes one.
    lines = copy_identity(lines, Path(vm_dir).name)
    old = _ini_get(lines, "Hard disks", "hdd_01_parameters")
    bus = old.split(",")[4].strip() if old and len(old.split(",")) >= 5 else "ide"
    lines = _ini_set(lines, "Hard disks", "hdd_01_fn", hdd_fn)
    lines = _ini_set(lines, "Hard disks", "hdd_01_parameters", f"{spt}, {heads}, {cyls}, 0, {bus}")
    if _ini_get(lines, "Hard disks", f"hdd_01_{bus}_channel") is None and bus == "ide":
        lines = _ini_set(lines, "Hard disks", "hdd_01_ide_channel", "0:0")
    if _ini_get(lines, "Hard disks", "hdd_01_speed") is None:
        lines = _ini_set(lines, "Hard disks", "hdd_01_speed", speed)
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


def machine_of(vm_dir: str | os.PathLike) -> str | None:
    """The machine's internal name from the VM's 86box.cfg (86Box names CMOS files after it)."""
    cfg = Path(vm_dir) / "86box.cfg"
    return _ini_get(cfg.read_text().splitlines(), "Machine", "machine") if cfg.exists() else None


def prime_nvr(vm_dir: str | os.PathLike, nvr: str | os.PathLike | None = None) -> str:
    """Put a saved CMOS into the VM, so the BIOS boots without stopping at setup.

    nvr may be a .nvr file (copied as nvr/<machine>.nvr, the name 86Box looks for) or a folder of
    them. Without one, the bundled CMOS is used, but only for the machine it was saved from.
    """
    d = Path(vm_dir)
    machine = machine_of(d)
    if nvr is None:
        bundled = PRIMED_NVR / f"{machine}.nvr"
        if not bundled.is_file():
            return f"no CMOS file for machine {machine!r}; the BIOS may stop at setup on first boot"
        src = bundled
    else:
        src = Path(nvr).expanduser()
        if src.is_dir():
            shutil.copytree(src, d / "nvr", dirs_exist_ok=True)
            return f"CMOS from {src}"
        if not src.is_file():
            raise FileNotFoundError(f"CMOS file {src} not found")
    (d / "nvr").mkdir(exist_ok=True)
    shutil.copyfile(src, d / "nvr" / f"{machine}.nvr")
    return f"CMOS from {src}" + (f" (as {machine}.nvr)" if src.name != f"{machine}.nvr" else "")


def _ini_set(lines: list[str], section: str, key: str, value: str) -> list[str]:
    """Set key in [section] of an 86Box-style INI held as lines, adding either if missing."""
    header = f"[{section}]"
    try:
        start = lines.index(header)
    except ValueError:
        if lines and lines[-1].strip():
            lines.append("")
        return lines + [header, f"{key} = {value}"]
    end = start + 1
    while end < len(lines) and not lines[end].startswith("["):
        if lines[end].split("=", 1)[0].strip() == key:
            lines[end] = f"{key} = {value}"
            return lines
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return lines[:end] + [f"{key} = {value}"] + lines[end:]


def _move_time_sync(lines: list[str]) -> list[str]:
    """Older versions of this server wrote time_sync under [General], where 86Box ignores it (it
    reads [Machine]), so those VMs' clocks followed the host's. Move it where it takes effect."""
    misplaced = _ini_get(lines, "General", "time_sync")
    if misplaced is None:
        return lines
    out, inside = [], False
    for line in lines:
        if line.startswith("["):
            inside = line.strip() == "[General]"
        elif inside and line.split("=", 1)[0].strip() == "time_sync":
            continue
        out.append(line)
    if _ini_get(out, "Machine", "time_sync") is None:
        out = _ini_set(out, "Machine", "time_sync", misplaced)
    return out


def _ini_get(lines: list[str], section: str, key: str) -> str | None:
    inside = False
    for line in lines:
        if line.startswith("["):
            inside = line.strip() == f"[{section}]"
        elif inside and line.split("=", 1)[0].strip() == key:
            return line.split("=", 1)[1].strip()
    return None


def configure_serial_automation(vm_dir: str | os.PathLike, port: int) -> bool:
    """Give COM<port> 86Box's automation backend (serialN_device = automation), so the automation
    socket carries the port both ways, in emulated time. Leaves the port alone, and returns False,
    if the VM attaches some other device to it (a serial mouse, a host port, a pipe)."""
    cfg = Path(vm_dir) / "86box.cfg"
    lines = cfg.read_text().splitlines()
    device = _ini_get(lines, "Ports (COM & LPT)", f"serial{port}_device")
    if device not in (None, "none", "file", "automation"):
        return False
    lines = _ini_set(lines, "Ports (COM & LPT)", f"serial{port}_enabled", "1")
    lines = _ini_set(lines, "Ports (COM & LPT)", f"serial{port}_device", "automation")
    _rewrite_cfg(cfg, lines)
    return True


def configure_serial_capture(vm_dir: str | os.PathLike, port: int = 1) -> Path | None:
    """Send COM<port> output to <vm_dir>/com<port>.log via 86Box's file backend.

    Leaves the port alone if the VM already attaches something else to it. Returns the log path,
    or None if the port is in use.
    """
    cfg = Path(vm_dir) / "86box.cfg"
    lines = cfg.read_text().splitlines()
    log = (Path(vm_dir) / f"com{port}.log").resolve()
    device = _ini_get(lines, "Ports (COM & LPT)", f"serial{port}_device")
    if device not in (None, "none", "file"):
        return None
    section = f"File (COM) #{port}"
    if device == "file" and _ini_get(lines, section, "path") not in (None, str(log)):
        return None  # the user's own file capture
    lines = _ini_set(lines, "Ports (COM & LPT)", f"serial{port}_device", "file")
    lines = _ini_set(lines, section, "path", str(log))
    # An input file makes the backend raise DSR/DCD; without them the BIOS (INT 14h) refuses to send
    # and DOS reports "Write fault error writing device COM1". The null device supplies no input.
    lines = _ini_set(lines, section, "input_path", os.devnull)
    # Write each byte through to the file, so serial_read sees it while the VM runs.
    lines = _ini_set(lines, section, "flush", "1")
    cfg.write_text("\n".join(lines) + "\n")
    return log


def _unlink_patiently(f: Path) -> None:
    """Delete f. On Windows a just-written file can be held open for a moment (by a virus
    scanner, say), so retry briefly, then leave it: it's a temporary file, and stop() mustn't fail."""
    for attempt in range(5):
        try:
            f.unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.2 * (attempt + 1))


def _rewrite_cfg(cfg: Path, lines: list[str]) -> None:
    """Write 86box.cfg back with the line endings it had (CRLF from 86Box on Windows)."""
    newline = "\r\n" if b"\r\n" in cfg.read_bytes() else "\n"
    cfg.write_text(newline.join(lines) + newline, newline="")


def _window_platform(env: dict) -> str:
    """QT_QPA_PLATFORM for a visible window outside Windows: native Wayland when there's a
    compositor, falling back to X11 (XWayland, on a Wayland desktop) if Qt's Wayland plugin is
    missing or can't connect. Qt tries a ';'-separated list in order."""
    platforms = [name for name, var in (("wayland", "WAYLAND_DISPLAY"), ("xcb", "DISPLAY")) if env.get(var)]
    if not platforms:
        raise RuntimeError("window=True needs a display: WAYLAND_DISPLAY or DISPLAY set")
    return ";".join(platforms)


def _free_port() -> int:
    """An unused loopback TCP port (86Box binds it a moment later)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tool(name: str) -> str:
    found = shutil.which(name) or (MTOOLS_DIR and shutil.which(name, path=MTOOLS_DIR))
    if not found:
        where = f"on PATH or in {MTOOLS_DIR}" if MTOOLS_DIR else "on PATH (install mtools, or set BOX86_MTOOLS_DIR)"
        raise FileNotFoundError(f"{name} not found {where}")
    return found


class VM:
    """One running 86Box process with an automation connection."""

    def __init__(self, vm_dir: str | os.PathLike, binary: Path = BINARY, roms: Path | None = ROMS):
        self.vm_dir = Path(vm_dir).expanduser().resolve()
        self.binary = Path(binary).expanduser()
        self.windowed = False
        self.roms = Path(roms).expanduser() if roms else None
        self.run_dir = Path(tempfile.mkdtemp(prefix="box86mcp-"))
        self.log_path = self.run_dir / "86box.log"
        self.proc: subprocess.Popen | None = None
        self.serial_logs: dict[int, Path] = {}
        self.serial_offsets: dict[int, int] = {}
        # COM ports on the automation backend: port -> where serial_read goes on from.
        self.serial_ports: dict[int, int] = {}
        # UNIX socket on Linux/macOS (owner-only); TCP on loopback where there are no UNIX sockets.
        self.transport = os.environ.get("BOX86_TRANSPORT", "tcp" if os.name == "nt" else "unix")
        if self.transport == "tcp":
            self.token_file = None if INSECURE else str(self.run_dir / "automation.token")
            self.endpoint = f"tcp:127.0.0.1:{_free_port()}"
        elif self.transport == "unix":
            if not hasattr(socket, "AF_UNIX"):
                raise ValueError("BOX86_TRANSPORT=unix: this Python has no UNIX sockets (Windows); use tcp")
            self.token_file = None
            self.endpoint = "unix:" + str(self.run_dir / "automation.sock")
        else:
            raise ValueError(f"BOX86_TRANSPORT must be unix or tcp, not {self.transport!r}")
        self.auto = Automation(self.endpoint, self.token_file)
        self._media_seq = 0
        self._init_debugger("127.0.0.1")

    def _init_debugger(self, stub_host: str) -> None:
        self.auto.on_debugger_stop = self._debugger_stop
        self.stub_host = stub_host
        self.gdb: Gdb | None = None
        self.gdb_port: int | None = None
        self.gdb_stopped = False       # the stub has the CPU stopped (a breakpoint, or a step)
        self._stub_let_go = False
        self._expect_stop = False      # inside a debugger step or run, a stop is the expected result
        self.breaks: set[tuple[int, str, int]] = set()   # (address, kind, length) set through the stub

    def start(self, held: bool = True, window: bool | None = None, extra_args: list[str] | None = None,
              com1_log: bool = False, debugger: bool = False, serial_ports: list[int] | None = None) -> dict:
        """Launch 86Box. window=True shows the normal 86Box window (outside Windows: on Wayland if
        there is a compositor, falling back to $DISPLAY; see _window_platform); the default, None,
        shows it on Windows and runs headless elsewhere (BOX86_WINDOW=1 or 0 sets it).
        com1_log=True captures COM1 output (off by default: with it, runs are not always
        byte-for-byte reproducible). debugger=True attaches to 86Box's GDB stub, for breakpoints,
        instruction steps and port I/O (86Box built with -D GDBSTUB=ON); see self.gdb. serial_ports:
        COM ports to give the automation backend (both directions over the automation socket; needs
        an 86Box with it), e.g. [1]; COM1 there takes precedence over com1_log."""
        if not (self.vm_dir / "86box.cfg").exists():
            raise FileNotFoundError(f"{self.vm_dir}/86box.cfg not found")
        if not self.binary.exists():
            raise FileNotFoundError(f"86Box binary {self.binary} not found (set BOX86_BINARY)")
        # A uuid that doesn't match the folder (a copied or moved VM) makes 86Box ask, before it
        # starts the machine, whether the VM was moved or copied; headless, that is a silent hang.
        # Without one, 86Box makes a new one and doesn't ask.
        cfg = self.vm_dir / "86box.cfg"
        lines = cfg.read_text().splitlines()
        kept = _move_time_sync([l for l in lines if l.split("=", 1)[0].strip() != "uuid"])
        if kept != lines:
            _rewrite_cfg(cfg, kept)
        if debugger:
            # Every 86Box stub listens on 12345 unless told otherwise; give this one its own port.
            cfg = self.vm_dir / "86box.cfg"
            _rewrite_cfg(cfg, _ini_set(cfg.read_text().splitlines(), "General", "gdbstub_port", str(_free_port())))
        self.serial_ports = {}
        for port in serial_ports or []:
            if not configure_serial_automation(self.vm_dir, port):
                raise ValueError(f"COM{port} already has a device in {self.vm_dir}/86box.cfg")
        if 1 in (serial_ports or []):
            com1_log = False
        log = configure_serial_capture(self.vm_dir, 1) if com1_log else None
        if log:
            self.serial_logs[1] = log
            self.serial_offsets[1] = 0
        # 86Box rewrites its global settings file at every start: give it one of its own, so a run
        # leaves the user's alone, with the confirmations off (86Box only reads them from there;
        # headless, they'd be dialogs nobody sees).
        global_cfg = self.run_dir / "86box_global.cfg"
        global_cfg.write_text("[Emulator]\nconfirm_exit = 0\nconfirm_reset = 0\nconfirm_save = 0\n")
        args = [str(self.binary), "-P", str(self.vm_dir), "-O", str(global_cfg), "-N", "--automation", self.endpoint]
        if self.roms:
            args += ["-R", str(self.roms)]
        if self.token_file:
            args += ["--automation-token-file", self.token_file]
        elif self.transport == "tcp":
            args.append("--automation-insecure")
        if held:
            args.append("--automation-hold")
        args += extra_args or []
        env = dict(os.environ)
        if window is None:
            default = os.environ.get("BOX86_WINDOW", "")
            window = default == "1" if default in ("0", "1") else os.name == "nt"
        self.windowed = window
        if window and os.name == "nt":
            env.pop("QT_QPA_PLATFORM", None)   # Windows: Qt's native platform shows the window
        elif window:
            env["QT_QPA_PLATFORM"] = _window_platform(env)
        else:
            env["QT_QPA_PLATFORM"] = "offscreen"
        log = open(self.log_path, "wb")
        # Through guard.py, which stops 86Box if this process dies without calling stop(): it holds
        # the read end of the stdin pipe, and only we hold the write end.
        guard = [sys.executable, str(Path(__file__).with_name("guard.py"))]
        if window and os.name == "nt":
            guard.append("--no-activate")   # show the window, but leave the focus where the user has it
        self._mem_bytes = None
        self.proc = subprocess.Popen(guard + args, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True,
                                     # Windows: its own process group, so a console Ctrl+C doesn't reach it.
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        log.close()
        try:
            self.auto.connect(wait=60)
        except AutomationError:
            self.stop()
            hint = ""
            if "Automation:" not in self.log_tail(400):       # an automation build logs that at startup
                hint = f" (is {self.binary} built with -D AUTOMATION=ON?)"
            raise AutomationError(f"86Box did not open its automation socket{hint}; log tail:\n{self.log_tail()}")
        status = self.auto.status()
        if status.get("protocol") not in SUPPORTED_PROTOCOLS:
            self.stop()
            raise AutomationError(f"86Box speaks automation protocol {status.get('protocol')}; "
                                  f"this server supports {sorted(SUPPORTED_PROTOCOLS)}")
        for port in serial_ports or []:
            try:
                self.serial_ports[port] = self.auto.serial_out(port, 0, 1)["pos"]
            except AutomationError as e:
                self.stop()
                if "unknown command" in str(e):
                    raise AutomationError(f"{self.binary} has no serial ports on its automation socket "
                                          "(an 86Box with serialN_device = automation is needed)") from e
                raise
        if debugger:
            self.attach_debugger()
        elif not held:
            self.let_cpu_run()
        return status

    # GDB stub ---------------------------------------------------------------

    def _stub_port(self, wait: float = 0.0) -> int | None:
        """The port 86Box's GDB stub listens on, from its log, or None if it has no stub (yet)."""
        deadline = time.monotonic() + wait
        while True:
            log = self.log_tail(400)
            m = re.search(r"GDB Stub: Listening on port (\d+)", log)
            if m:
                return int(m.group(1))
            if "GDB Stub: Failed to bind" in log or time.monotonic() >= deadline:
                return None
            time.sleep(0.05)

    def attach_debugger(self) -> Gdb:
        """Connect to the GDB stub and let the CPU run under automation control as usual."""
        # The stub starts when the machine does, just after the automation socket.
        port = self._stub_port(wait=10)
        if port is None:
            raise AutomationError("no GDB stub: this needs 86Box built with -D GDBSTUB=ON "
                                  f"(and a free port for it); log tail:\n{self.log_tail(10)}")
        self.gdb_port = port
        self.gdb = Gdb(self.stub_host, port)
        self.gdb.cont()        # the stub starts the CPU stopped
        self.gdb_stopped = False
        return self.gdb

    @contextlib.contextmanager
    def expecting_stop(self):
        """Within this, a timed command that ends because the debugger stopped the CPU just returns."""
        self._expect_stop = True
        try:
            yield
        finally:
            self._expect_stop = False

    def let_cpu_run(self) -> None:
        """Before running in real time: a GDB-stub build starts the CPU stopped, and only a timed
        command would notice. Without a debugger, let it go now (looked up once per VM)."""
        if self.gdb is not None or self._stub_let_go:
            return
        port = self._stub_port(wait=1)
        self._stub_let_go = True
        if port is not None:
            try:
                Gdb(self.stub_host, port).close()
            except OSError:
                pass

    def _debugger_stop(self, request: str, reply: dict) -> dict:
        if self.gdb is not None:
            if self._expect_stop:
                self.gdb_stopped = True
                return reply
            what = request.split()[0]
            if self.gdb_stopped:
                raise AutomationError(f"{what}: the debugger has the CPU stopped, so no time can pass; "
                                      "call debug_continue, run_until_break or step_instructions first")
            self.gdb_stopped = True
            why = describe_stop(self.gdb.recv(timeout=10))
            raise AutomationError(f"{what}: a {why} at linear {self.gdb.regs()['eip']:#x} stopped the CPU, "
                                  f"at emulated t={reply.get('ms')}ms; it stays stopped until "
                                  "debug_continue, run_until_break or step_instructions")
        # An 86Box built with the GDB stub starts the CPU stopped until a debugger lets it go, and a
        # debugger that connects and leaves does. Then redo the request; no time has passed.
        port = self._stub_port(wait=2)
        if port is None or self._stub_let_go:
            return reply
        self._stub_let_go = True
        try:
            Gdb(self.stub_host, port).close()
        except OSError:
            return reply
        time.sleep(0.2)
        return self.auto.call(request)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _stop_puller(self) -> None:
        """End a sound recording's pull thread (record_stop wasn't called)."""
        puller = getattr(self, "puller", None)
        if puller is not None:
            puller._done.set()
            self.puller = None
            self.auto.after_reply = None
            self.auto.chunk_ms = None

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_puller()
        if self.proc and self.proc.poll() is None:
            try:
                self.auto.call("quit")
            except (AutomationError, OSError):
                pass
            # Hang up before waiting: on Windows, 86Box's exit waits for its automation thread, which
            # sits in recv() until the client goes (shutdown() doesn't wake it there).
            self.auto.close()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    # No process groups, and killing the guard alone would leave 86Box running:
                    # close the guard's stdin, so that it stops 86Box (terminate, then kill)...
                    if self.proc.stdin:
                        self.proc.stdin.close()
                    try:
                        self.proc.wait(GUARD_GRACE_S + 5)
                    except subprocess.TimeoutExpired:
                        # ...and if even that fails, kill the guard and everything it started.
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                                       capture_output=True)
                        self.proc.wait()
                else:
                    # SIGTERM first: docker/86box.sh traps it to kill its container.
                    os.killpg(self.proc.pid, signal.SIGTERM)
                    try:
                        self.proc.wait(10)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                        self.proc.wait()
        if self.gdb is not None:
            self.gdb.close()
            self.gdb = None
        if self.proc and self.proc.stdin:
            self.proc.stdin.close()
        self.auto.close()
        # Media copies, the socket and the token aren't needed once 86Box has gone; the log stays.
        for f in self.run_dir.iterdir():
            if f.name == self.log_path.name:
                continue
            if f.is_dir() and not f.is_symlink():
                shutil.rmtree(f, ignore_errors=True)
            else:
                _unlink_patiently(f)

    def power_off_soft(self, timeout: float = 60.0) -> bool:
        """Press the power button and let the guest shut itself down, in real time. True once 86Box
        has exited (and stop() has tidied up); False if it's still running after timeout seconds,
        released, because the guest ignored the button or hasn't finished."""
        # Run in real time first, then press, then hang up: nothing may follow quit soft. Without a
        # power button 86Box powers off at once, and on Windows its listener still reads (see stop),
        # takes the next request and waits forever for a CPU that has stopped. Calls after a False
        # return reconnect.
        self.auto.check_quit_soft()
        self.let_cpu_run()
        self.auto.release()
        self.auto.call("quit soft")
        self.auto.close()
        if not self._wait_exit(timeout):
            return False
        self.stop()
        return True

    def _wait_exit(self, timeout: float) -> bool:
        try:
            self.proc.wait(timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def mem_bytes(self) -> int:
        """Emulated RAM in bytes, from 86Box's own report (cached; RAM can't change while it runs)."""
        if getattr(self, "_mem_bytes", None) is None:
            self._mem_bytes = int(self.auto.info()["mem_kb"]) * 1024
        return self._mem_bytes

    def hdd_image(self) -> Path:
        """Path of the primary hard disk image named in 86box.cfg."""
        for line in (self.vm_dir / "86box.cfg").read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "hdd_01_fn":
                p = Path(value.strip())
                return p if p.is_absolute() else self.vm_dir / p
        raise FileNotFoundError("no hdd_01_fn in 86box.cfg")

    def serial_read(self, port: int = 1, from_start: bool = False) -> str:
        """Text the guest wrote to COM<port> since the last read (or since boot)."""
        if port in self.serial_ports:
            r = self.auto.serial_out(port, 0 if from_start else self.serial_ports[port])
            self.serial_ports[port] = r["next"]
            data = r["data"]
            if r["lost"]:
                data = f"[{r['lost']} bytes lost: more than 64 KiB arrived between reads]\n".encode() + data
            return data.decode("latin-1")
        log = self.serial_logs.get(port)
        if log is None:
            raise RuntimeError(f"COM{port} is not captured: start the VM with serial=[{port}] "
                               f"(or com1_log=True for COM1 output only)")
        offset = 0 if from_start else self.serial_offsets.get(port, 0)
        try:
            with open(log, "rb") as f:
                f.seek(offset)
                data = f.read()
        except FileNotFoundError:
            return ""
        self.serial_offsets[port] = offset + len(data)
        return data.decode("latin-1")

    def log_tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log_path.read_text(errors="replace").splitlines()[-lines:])
        except FileNotFoundError:
            return ""

    # Media ---------------------------------------------------------------

    def make_floppy(self, files: list[str | os.PathLike]) -> Path:
        """Build a 1.44 MB FAT12 floppy image holding the given files and directories."""
        self._media_seq += 1
        img = self.run_dir / f"transfer{self._media_seq}.img"
        with open(img, "wb") as f:
            f.truncate(1474560)
        subprocess.run([_tool("mformat"), "-i", str(img), "-f", "1440", "-N", VOLUME_SERIAL, "::"],
                       check=True, capture_output=True, env=MTOOLS_ENV)
        for src in files:
            src = Path(src).expanduser()
            cmd = [_tool("mcopy"), "-i", str(img), "-s", "-o", "-m", str(src), "::"]
            r = subprocess.run(cmd, capture_output=True, text=True, env=MTOOLS_ENV)
            if r.returncode:
                raise RuntimeError(f"mcopy {src} failed: {r.stderr.strip()} (floppy holds 1.44 MB)")
        return img

    def make_iso(self, files: list[str | os.PathLike], label: str = "TRANSFER") -> Path:
        """Build an ISO 9660 image (with Joliet long names) holding the given files and directories."""
        import pycdlib

        self._media_seq += 1
        iso_path = self.run_dir / f"transfer{self._media_seq}.iso"
        iso = pycdlib.PyCdlib()
        iso.new(interchange_level=1, joliet=3, vol_ident=label[:32].upper())
        used: set[str] = set()

        def dos_name(name: str, is_dir: bool) -> str:
            stem, _, ext = name.upper().rpartition(".") if "." in name and not is_dir else (name.upper(), "", "")
            clean = lambda s, n: "".join(c if c.isalnum() or c == "_" else "_" for c in s)[:n] or "_"
            base, ext = clean(stem, 8), clean(ext, 3) if ext else ""
            candidate, i = base, 1
            while (candidate + ("." + ext if ext else "")) in used:
                suffix = f"~{i}"
                candidate, i = base[: 8 - len(suffix)] + suffix, i + 1
            full = candidate + ("." + ext if ext else "")
            used.add(full)
            return full

        def add(src: Path, iso_dir: str, joliet_dir: str) -> None:
            short = dos_name(src.name, src.is_dir())
            iso_name, joliet_name = f"{iso_dir}/{short}", f"{joliet_dir}/{src.name}"
            if src.is_dir():
                iso.add_directory(iso_name, joliet_path=joliet_name)
                for child in sorted(src.iterdir()):
                    add(child, iso_name, joliet_name)
            else:
                iso.add_file(str(src), iso_name + ";1", joliet_path=joliet_name)

        for f in files:
            src = Path(f).expanduser()
            if not src.exists():
                raise FileNotFoundError(src)
            add(src, "", "")
        iso.write(str(iso_path))
        iso.close()
        return iso_path

    def visible(self, path: str | os.PathLike) -> Path:
        """path, as 86Box can open it. The Docker wrapper mounts only $HOME, /tmp and the repo, so
        files elsewhere are copied into the run directory first; a native 86Box sees everything."""
        path = Path(path).expanduser().resolve()
        dockerised = self.binary.suffix == ".sh"      # docker/86box.sh, or a copy of it for another build
        if dockerised and not any(path.is_relative_to(p) for p in (Path.home(), Path("/tmp"))):
            copy = self.run_dir / path.name
            shutil.copy(path, copy)
            if path.suffix.lower() == ".cue":          # and the track files the cue sheet names
                for name in re.findall(r'^\s*FILE\s+"([^"]+)"', path.read_text(errors="replace"), re.M | re.I):
                    shutil.copy(path.parent / name, self.run_dir / Path(name).name)
            path = copy
        return path

    def media(self, path: str | os.PathLike) -> str:
        """An image file, as 86Box can open it."""
        return str(self.visible(path))      # str: Path would collapse "//"

    def load_cdrom(self, image: str | os.PathLike, drive: int = 0) -> dict:
        """Insert a CD image."""
        return self.auto.call(f"cd load {drive} {self.media(image)}")

    def load_floppy(self, img: str | os.PathLike, drive: int = 0, write_protect: bool = False) -> dict:
        """Insert a floppy image. 86Box answers ok even when it can't load one, so check it's in."""
        img = ("wp://" if write_protect else "") + self.media(img)
        reply = self.auto.call(f"fdd load {drive} {img}")
        if not any(f["drive"] == drive for f in self.auto.info()["floppy"]):
            raise AutomationError(f"86Box did not load the floppy image {img}; the log may say why:\n"
                                  f"{self.log_tail(5)}")
        return reply



def _media_name(name: str) -> str:
    """A file name as media put can carry it: one word, with no path in it. The OS there judges the rest."""
    name = re.sub(r"[\s/\\:]", "_", name)
    return "_" if name in ("", ".", "..") else name


class AttachedVM(VM):
    """An 86Box started elsewhere (another machine, or by hand), reached at an endpoint.

    vm_stop disconnects rather than powering it off.
    """

    def __init__(self, endpoint: str, token: str | None = None, gdb_port: int | None = None):
        self.vm_dir = None
        self._gdb_port = gdb_port
        self._sent: dict[tuple, str] = {}   # files sent with media put: (path, size, mtime) -> its path there
        self._can_send: bool | None = None
        self.run_dir = Path(tempfile.mkdtemp(prefix="box86mcp-attach-"))
        self.log_path = self.run_dir / "86box.log"
        self.proc = None
        self.serial_logs = {}
        self.serial_offsets = {}
        self._media_seq = 0
        self.endpoint = endpoint
        self.token_file = None
        if endpoint.startswith("tcp:") and token:
            self.token_file = str(self.run_dir / "automation.token")
            fd = os.open(self.token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token.strip() + "\n")
        self.auto = Automation(endpoint, self.token_file)
        self._init_debugger(endpoint[4:].rsplit(":", 1)[0] if endpoint.startswith("tcp:") else "127.0.0.1")
        self.auto.connect(wait=10)
        # Its COM ports on the automation backend, read on from where they are now.
        self.serial_ports = {p["port"]: p["pos"] for p in self.auto.info().get("serial", [])
                             if p.get("device") == "automation" and "pos" in p}

    def _stub_port(self, wait: float = 0.0) -> int | None:
        # As given to vm_attach, else as 86Box says (2026-10-22 on), else the stub's default.
        if self._gdb_port:
            return self._gdb_port
        try:
            return self.auto.info().get("gdbstub_port") or 12345
        except (AutomationError, OSError):
            return 12345

    def log_tail(self, lines: int = 40) -> str:
        """The end of 86Box's log, over the connection (an 86Box from 2026-10-22 on)."""
        try:
            text = self.auto.log()["text"]
        except (AutomationError, OSError):
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def media(self, path: str | os.PathLike) -> str:
        """An image file here is sent to 86Box over the connection (media put), and 86Box opens its
        copy; any other path, or with an 86Box from before 2026-10-22, is a path on its machine."""
        local = Path(path).expanduser()
        if not local.is_file():
            return str(path)
        if self._can_send is None:
            self._can_send = self.auto.can_put_files()
        if not self._can_send:
            return str(path)
        local = local.resolve()
        if local.suffix.lower() == ".cue":
            return self._send_cue(local)
        return self._send(local)

    def _send(self, local: Path, name: str | None = None) -> str:
        st = local.stat()
        key = (str(local), st.st_size, st.st_mtime_ns, name)
        if key not in self._sent:
            self._sent[key] = self.auto.put_file(name or _media_name(local.name), local)["path"]
        return self._sent[key]

    def _send_cue(self, cue: Path) -> str:
        """A cue sheet and the track files it names, under names media put can carry (no spaces),
        with the sheet rewritten to match."""
        text = cue.read_text(errors="replace")

        def track(m: re.Match) -> str:
            name = _media_name(Path(m.group(2)).name)
            self._send(cue.parent / m.group(2), name)
            return f'{m.group(1)}"{name}"'

        text = re.sub(r'^(\s*FILE\s+)"([^"]+)"', track, text, flags=re.M | re.I)
        sheet = self.run_dir / _media_name(cue.name)
        sheet.write_text(text)
        return self._send(sheet)

    def running(self) -> bool:
        return self.auto.sock is not None

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_puller()
        self.auto.close()

    def power_off(self) -> None:
        """Ask 86Box to power off and exit (the "quit" command), then disconnect."""
        try:
            self.auto.call("quit")
        except (AutomationError, OSError):
            pass  # it may close the connection as it exits
        self.auto.close()

    def _wait_exit(self, timeout: float) -> bool:
        # No process to wait for: 86Box has gone when the connection fails. Hang up between polls,
        # or 86Box can't exit on Windows (see VM.stop).
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.auto.status()
            except (AutomationError, OSError):
                return True
            finally:
                self.auto.close()
            time.sleep(0.2)
        return False

    def hdd_image(self) -> Path:
        raise RuntimeError("the disk image of an attached VM is on the other machine")
