"""MCP server exposing a headless 86Box machine as tools."""

from __future__ import annotations

import functools
import inspect
import io
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PILImage

from . import disk
from . import keys as K
from . import x86
from .gdbrsp import BREAK_KINDS, Gdb, describe_stop
from . import serialpeers
from .protocol import AutomationError
from .vm import VM, machine_of, prime_nvr, write_config

mcp = MCPServer(
    "86box",
    instructions=(
        "Drives a PC emulated by 86Box. vm_start boots a VM (vm_create or dos_create_vm makes one). "
        "Time: by default the machine is HELD, so emulated time only advances inside tool calls (step, act, "
        "press_keys, type_text, wait_for_text, dos_run): a real-time program waits while you think, and runs "
        "are repeatable. vm_start(held=False), or release, runs it in real time instead, for a person "
        "watching, a server or agent inside the guest, or real-time behaviour; hold goes back. "
        "measure_speed reports emulated speed: paced (does it keep up with real time?) or unpaced (raw "
        "throughput, for comparing builds). vm_start(binary=...) picks the 86Box executable for that start, "
        "so one session can compare builds. Prefer screen_text over screenshot in text modes; it is cheaper "
        "and exact. A failing tool says why. Several VMs: name them with vm=... on any tool. Held VMs are "
        "linked in groups (all in \"default\" unless vm_group/vm_start(group=) says otherwise): time "
        "passing on one (any tool) passes on every held VM of its group, in 10 ms lockstep quanta, so "
        "networked guests stay in step. step_all runs a group; record_stop_all renders its recordings "
        "side by side on the shared clock."
    ),
)



class _VMState:
    """What the tools keep about one VM. The selected VM's state lives in the module globals below,
    which every tool uses; _select() swaps another VM's in."""

    def __init__(self):
        self.vm: VM | None = None
        self.held = True
        self.mouse_state: dict = {}
        self.record_dirs: list[Path] = []
        self.snapshots: dict[str, dict] = {}
        self.last_dir: Path | None = None    # the VM folder last created or started under this name


_vms: dict[str, _VMState] = {}          # by name; "main" unless a tool names another
_current = "main"
_lock = threading.RLock()               # one tool call at a time: the globals belong to one VM
_groups: dict[str, str] = {}            # VM name -> its group; held VMs in a group step together
_quantum_ms = 10                        # their lockstep quantum


def _group_of(name: str) -> str:
    return _groups.get(name, "default")


def _save_current() -> None:
    st = _vms.setdefault(_current, _VMState())
    st.vm, st.held, st.mouse_state, st.record_dirs, st.snapshots, st.last_dir = (
        _vm, _held, _mouse_state, _record_dirs, _snapshots, _last_dir)


def _select(name: str) -> None:
    global _current, _vm, _held, _mouse_state, _record_dirs, _snapshots, _last_dir
    if name == _current:
        return
    _save_current()
    st = _vms.setdefault(name, _VMState())
    _current = name
    _vm, _held, _mouse_state, _record_dirs, _snapshots, _last_dir = (
        st.vm, st.held, st.mouse_state, st.record_dirs, st.snapshots, st.last_dir)


VM_PARAM_DOC = ("\n\nvm: which VM, by name, when the server runs more than one (vm_start(vm=\"p2\", ...) starts "
                "one called p2). By default the one last named; \"main\" if none has been.")


def tool():
    """Register a function as an MCP tool, with an optional vm argument that picks the VM it acts on.

    Whatever the tool raises reaches the client as the error's own message: the MCP library passes on
    only a ToolError's text, and reports anything else as just "Error executing tool X". The function
    itself is returned unchanged, so Python callers still use the selected VM and see the original
    exceptions."""
    register = mcp.tool()

    def decorate(fn):
        sig = inspect.signature(fn, eval_str=True)

        @functools.wraps(fn)
        def wrapper(*args, vm: str | None = None, **kwargs):
            with _lock:
                try:
                    if vm is not None:
                        _select(vm)
                    return fn(*args, **kwargs)
                except ToolError:
                    raise
                except (AutomationError, ValueError, FileNotFoundError) as e:
                    raise ToolError(str(e)) from e
                except Exception as e:      # still worth seeing: say what kind of failure it was
                    raise ToolError(f"{type(e).__name__}: {e}") from e
                finally:
                    _save_current()

        if "vm" not in sig.parameters:
            wrapper.__signature__ = sig.replace(parameters=[
                *sig.parameters.values(),
                inspect.Parameter("vm", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None)])
            wrapper.__doc__ = (fn.__doc__ or "") + VM_PARAM_DOC
        register(wrapper)
        return fn

    return decorate


_vm: VM | None = None
_held = True


def vm() -> VM:
    if _vm is None or not _vm.running():
        raise AutomationError("no VM running; call vm_start first")
    return _vm


def _advance(ms: int) -> None:
    """Let ms of emulated time pass: exactly, if held; approximately, if free-running."""
    if ms <= 0:
        return
    if _held:
        _step(ms)
    else:
        time.sleep(ms / 1000)


def _step(ms: int) -> dict:
    """Step the selected VM ms, and every other held VM in its group with it, in lockstep."""
    states = [(n, st) for n, st in _group_states(_group_of(_current)) if st.held]
    if (len(states) > 1 or _has_serial(_current)) and any(n == _current for n, _ in states):
        _run_in_lockstep(states, ms, _quantum_ms)
        return vm().auto.status()
    return vm().auto.step(ms)


def _wait(native, timeout_ms: int, carry=None) -> dict:
    """Run a native wait (native(timeout_ms) -> reply with "matched") on the selected VM. With other
    held VMs in its group, they step with it a quantum at a time, concurrently; when the wait is
    satisfied part way through a quantum, the selected VM is brought up to the others' time.
    carry(reply) is called between quanta, for waits that continue from where the last left off."""
    from concurrent.futures import ThreadPoolExecutor

    states = [(n, st) for n, st in _group_states(_group_of(_current)) if st.held]
    if (len(states) <= 1 and not _has_serial(_current)) or not any(n == _current for n, _ in states):
        return native(timeout_ms)
    me = vm()
    others = [st for n, st in states if n != _current]
    waited = 0
    with ThreadPoolExecutor(len(states)) as pool:
        while True:
            q = max(1, min(_quantum_ms, timeout_ms - waited))
            before = me.auto.status()["ms"]
            jobs = [pool.submit(st.vm.auto.step, q) for st in others]
            r = native(q)
            for j in jobs:
                j.result()
            ran = r["ms"] - before
            if ran < q:
                me.auto.step(q - ran)
            _pump_serial(states)
            waited += q
            if r.get("matched") or waited >= timeout_ms:
                return r
            if carry:
                carry(r)


def _snapshot(max_width: int) -> tuple[bytes, dict]:
    v = vm()
    info = v.auto.screenshot()
    img = PILImage.open(io.BytesIO(info.pop("png")))
    img.load()
    if max_width and img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), PILImage.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), info


def _time_note(info: dict) -> str:
    note = f"emulated t={info.get('ms')}ms frame={info.get('frames')}"
    if info.get("stopped") == "debugger":
        note += "; STOPPED: 86Box's GDB stub has the CPU stopped"
    return note


# Lifecycle -----------------------------------------------------------------




@tool()
def vm_create(vm_dir: str, cylinders: int = 993, heads: int = 16, sectors: int = 63,
              config: str | None = None, nvr: str | None = None) -> str:
    """Create a VM in vm_dir with a blank hard disk, ready for installing an OS from floppy or CD.

    The machine is a 486DX2-66 with 16 MB, unless config names an 86box.cfg to copy: then that
    machine, with its first hard disk replaced by the new blank one. nvr is a saved CMOS (.nvr file,
    or a folder of them) so the BIOS boots without stopping at setup; by default the bundled one is
    used when the machine is the default 486DX2-66. The default geometry, 993/16/63, matches a 512 MB
    CompactFlash card. To boot from media: vm_start (held, so the machine hasn't run yet), then
    floppy_insert or cdrom_insert, then step. The bundled CMOS boots from C: first, so once an
    installer has partitioned the disk, change the boot sequence in BIOS setup to boot the floppy.
    """
    d = Path(vm_dir).expanduser()
    if (d / "86box.cfg").exists():
        raise FileExistsError(f"{d} already has an 86box.cfg")
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "hdd.img", "wb") as f:
        f.truncate(cylinders * heads * sectors * 512)
    write_config(d, cylinders, heads, sectors, template=config)
    cmos = prime_nvr(d, nvr)
    global _last_dir
    _last_dir = d.resolve()
    mb = cylinders * heads * sectors * 512 // (1024 * 1024)
    return f"created {d} ({machine_of(d)}) with a blank {mb} MB disk ({cylinders}/{heads}/{sectors}); {cmos}"


@tool()
def dos_create_vm(vm_dir: str, base_image: str | None = None, cylinders: int = 993,
                  heads: int = 16, sectors: int = 63, autoexec: str = "@ECHO OFF\nPROMPT $P$G\n",
                  config: str | None = None, nvr: str | None = None) -> str:
    """Create a new VM in vm_dir with a fresh bootable DOS hard disk.

    The machine is a 486DX2-66 with 16 MB, unless config names an 86box.cfg to copy (its first hard
    disk becomes the new DOS disk). nvr: a saved CMOS file or folder, as for vm_create.

    The default geometry, 993/16/63, matches a 512 MB CompactFlash card. The disk uses 86Box's RAM-disk
    speed (no seek or rotation delays). DOS system files are taken from base_image, a bootable DOS hard
    disk image (default: $BOX86_DOS_BASE_IMG).
    """
    base_image = base_image or os.environ.get("BOX86_DOS_BASE_IMG")
    if not base_image:
        raise ValueError("pass base_image (a bootable DOS hard disk image) or set BOX86_DOS_BASE_IMG")
    d = Path(vm_dir).expanduser()
    if (d / "86box.cfg").exists():
        raise FileExistsError(f"{d} already has an 86box.cfg")
    d.mkdir(parents=True, exist_ok=True)
    img = d / "hdd.img"
    disk.make_dos_disk(img, Path(base_image).expanduser(), (cylinders, heads, sectors))
    disk.write_text(img, "AUTOEXEC.BAT", autoexec)
    write_config(d, cylinders, heads, sectors, template=config)
    cmos = prime_nvr(d, nvr)
    global _last_dir
    _last_dir = d.resolve()
    mb = cylinders * heads * sectors * 512 // (1024 * 1024)
    return f"created {d} ({machine_of(d)}) with a {mb} MB DOS disk ({cylinders}/{heads}/{sectors}); {cmos}"


@tool()
def dos_install_files(paths: list[str], dest_dir: str = "\\", vm_dir: str | None = None) -> str:
    """Copy host files/directories onto the VM's C: drive, e.g. dest_dir="\\GAME".

    The VM must be stopped (vm_stop): 86Box caches the disk, so writing to it while running corrupts it.
    No size limit, unlike dos_put_floppy. vm_dir defaults to the last VM started.
    """
    if _vm is not None and _vm.running():
        raise AutomationError("stop the VM first (vm_stop); then dos_install_files, then vm_start")
    target = VM(vm_dir) if vm_dir else _vm
    if target is None:
        raise ValueError("no VM known yet; pass vm_dir")
    copied = disk.put_files(target.hdd_image(), paths, dest_dir)
    return f"copied {', '.join(copied)} to C:{dest_dir}"


_binary: Path | None = None     # chosen with vm_start(binary=...); sticks for the session


def _find_binary(given: str) -> Path:
    """An 86Box executable, from a path to one, a bare name on PATH (86Box-nightly.exe), a CMake build
    folder, or an 86Box checkout."""
    path = Path(given).expanduser()
    if not path.is_absolute():
        on_path = shutil.which(given) if not (Path.cwd() / path).exists() and os.sep not in given \
            and "/" not in given else None
        path = Path(on_path) if on_path else Path.cwd() / path
    exe = "86Box.exe" if os.name == "nt" else "86Box"
    if path.is_dir():
        candidates = [path / "src" / exe, path / exe] + sorted(path.glob(f"*/src/{exe}"))
        found = next((c for c in candidates if c.is_file()), None)
        if found is None:
            raise FileNotFoundError(f"no {exe} in {path}: looked for src/{exe}, {exe} and */src/{exe} "
                                    "(a build folder, or a checkout with one inside)")
        return found
    if not path.exists():
        raise FileNotFoundError(f"86Box binary {path} not found")
    return path


def _state_file() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CACHE_HOME")
    return Path(base or Path.home() / ".cache").expanduser() / "box86mcp" / "last_vm"


def _remember_vm_dir(vm_dir: Path) -> None:
    try:
        f = _state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(vm_dir) + "\n")
    except OSError:
        pass        # only a convenience


_last_dir: Path | None = None   # the folder the selected VM last created or started


def _default_vm_dir() -> str:
    if _last_dir is not None:
        return str(_last_dir)
    if _vm is not None and _vm.vm_dir is not None:
        return str(_vm.vm_dir)
    if os.environ.get("BOX86_VM_DIR"):
        return os.environ["BOX86_VM_DIR"]
    try:
        last = _state_file().read_text().strip()
        if last and (Path(last) / "86box.cfg").exists():
            return last
    except OSError:
        pass
    raise ValueError("no VM to start: pass vm_dir, a folder with an 86box.cfg (vm_create or dos_create_vm "
                     "makes one), or set BOX86_VM_DIR")


@tool()
def vm_start(vm_dir: str | None = None, held: bool = True, window: bool | None = None, com1_log: bool = False,
             debugger: bool = False, binary: str | None = None, group: str | None = None,
             serial: list[int] | None = None) -> str:
    """Boot the 86Box machine whose 86box.cfg lives in vm_dir. Without vm_dir: the VM this session
    last created or started, else $BOX86_VM_DIR, else the last VM started by any session.

    held=True (default) freezes emulated time between tool calls, for deterministic, turn-based control.
    window=True shows the 86Box window on the host's display so a person can watch (and also type),
    and window=False runs without one. By default the window shows on Windows, and not elsewhere
    (BOX86_WINDOW=1 or 0 sets the default).
    serial: COM ports to carry over the automation socket, both ways and in emulated time, e.g. [1]
    or [1, 2]: serial_read, wait_for_serial and serial_write use them, and serial_attach and
    serial_link put something at the other end. Needs an 86Box with the automation serial backend.
    com1_log=True, for an 86Box without it, captures COM1 output only, through a file; it's off by
    default because with it, runs aren't always exactly reproducible.
    debugger=True attaches to 86Box's GDB stub for breakpoint_set, run_until_break,
    step_instructions, debug_continue, io_read/io_write and the PCI tools; it needs 86Box built with
    -D GDBSTUB=ON.
    binary: the 86Box to run (built with -D AUTOMATION=ON): the executable's path or its name on PATH,
    or a CMake build folder or 86Box checkout to find it in (build/src/86Box, for a worktree). Paths
    are absolute, or relative to the server's working directory. It sticks for the rest of the session; by default $BOX86_BINARY. To compare or
    bisect builds: vm_start(binary=A), measure, vm_stop, then binary=B.
    group: the lockstep group (see vm_group); by default the VM's earlier one, else "default", so
    VMs started together step together.
    """
    global _vm, _held, _last_dir
    if group is not None:
        _groups[_current] = group
    if vm_dir is None:
        vm_dir = _default_vm_dir()
    if _vm is not None and _vm.running():
        _vm.stop()
    global _binary
    if binary:
        _binary = _find_binary(binary)
    _vm = VM(vm_dir, binary=_binary) if _binary else VM(vm_dir)
    _detach_serial(_current)
    status = _vm.start(held=held, window=window, com1_log=com1_log, debugger=debugger, serial_ports=serial)
    _held = held
    _last_dir = _vm.vm_dir
    _remember_vm_dir(_vm.vm_dir)
    extras = (", windowed" if _vm.windowed else "") + (f", debugger on port {_vm.gdb_port}" if debugger else "")
    return (f"86Box {status.get('version')} ({_vm.binary}) running from {vm_dir} "
            f"({'held' if held else 'real-time'}{extras}). Log: {_vm.log_path}")


@tool()
def vm_attach(endpoint: str, token: str | None = None, gdb_port: int | None = None) -> str:
    """Control an 86Box that is already running, possibly on another machine, instead of starting one.

    endpoint: "tcp:<host>:<port>" or "unix:<path>". token: the contents of the token file that 86Box
    wrote at startup (for TCP, unless 86Box runs with --automation-insecure). gdb_port: its GDB stub's
    port, for the debugger tools, if 86Box doesn't say (86Box from before 2026-10-22; default 12345);
    the stub is reached on the endpoint's host.

    Over the connection, with an 86Box from 2026-10-22 or later: media (an image file on this
    machine given to floppy_insert or cdrom_insert, a .cue with its tracks, and the images
    dos_put_floppy and cdrom_insert(files=) build) is sent to 86Box, into its VM directory's
    automation-media folder, and vm_status's log comes from 86Box. A path that isn't a file here,
    or any path with an older 86Box, is a path on 86Box's machine. A floppy the guest writes to
    changes the copy there, not the file here. Screenshots, and recordings (frames and sound, with an
    86Box from 2026-10-20 or later), come back over the connection: record_start's directory is on
    this machine. vm_create, dos_create_vm and dos_install_files work on VM folders on this machine
    only.
    """
    global _vm, _held
    if _vm is not None and _vm.running():
        _vm.stop()
    from .vm import AttachedVM

    _vm = AttachedVM(endpoint, token, gdb_port)
    status = _vm.auto.status()
    _held = bool(status.get("held"))
    info = _vm.auto.info()
    return (f"attached to 86Box {status.get('version')} at {endpoint}: {info.get('machine_name')}, "
            f"{'held' if _held else 'running in real time'}, emulated t={status.get('ms')}ms")


@tool()
def vm_stop(power_off: bool = True, soft: bool = False, timeout_s: int = 60) -> str:
    """Power off the emulated machine and exit 86Box.

    soft=True presses the power button instead, like 86Box's "Power off (soft)": the machine runs in
    real time until the guest shuts itself down (an ACPI OS such as Windows 98 does), for up to
    timeout_s seconds. If it hasn't by then, it's left running, released; call vm_stop again to cut
    the power. A machine with no power button at all is just powered off.
    For a VM joined with vm_attach, power_off=False just disconnects and leaves it running.
    """
    global _held
    from .vm import AttachedVM

    if _vm is None or not _vm.running():
        return "no VM running"
    _detach_serial(_current)
    if soft and power_off:
        stopped = _vm.power_off_soft(timeout_s)
        _held = False
        if not stopped:
            return (f"pressed the power button, but 86Box is still running after {timeout_s}s; the machine "
                    "is released. vm_stop() cuts the power")
        return "powered off (soft); 86Box exited"
    if isinstance(_vm, AttachedVM):
        if not power_off:
            _vm.stop()
            return "disconnected; the VM is still running"
        _vm.power_off()
        return "powered off the attached VM (86Box exited)"
    _vm.stop()
    return "stopped"


@tool()
def vm_status() -> dict:
    """Emulated time, frame count, held state, video mode, and the tail of the 86Box log."""
    v = vm()
    s = v.auto.status()
    s.pop("ok", None)
    s["log_tail"] = v.log_tail(10)
    return s


@tool()
def vm_info() -> dict:
    """What the running emulator actually is: 86Box version, machine, CPU and clock, RAM, video and
    sound cards, mounted disk/floppy/CD images, emulation speed, and resets so far.

    hard_resets/soft_resets count every CPU reset, including ones the guest causes itself (a triple
    fault, or a reboot through the keyboard controller). A soft_resets count that goes up unexpectedly
    usually means a crash. speed_percent is measured while running; 100 = real time.
    """
    return vm().auto.info()


def _running_states() -> list[tuple[str, _VMState]]:
    _save_current()
    return [(n, st) for n, st in _vms.items() if st.vm is not None and st.vm.running()]


def _group_states(group: str | None) -> list[tuple[str, _VMState]]:
    """The running VMs of a group; None: the current VM's group."""
    group = _group_of(_current) if group is None else group
    return [(n, st) for n, st in _running_states() if _group_of(n) == group]


def _set_held(name: str, st: _VMState, held: bool) -> None:
    global _held
    st.held = held
    if name == _current:
        _held = held


def _align(states) -> dict[str, int]:
    """Hold every VM, then step the ones behind up to the one furthest along. Returns the times."""
    for name, st in states:
        st.vm.auto.hold()
        _set_held(name, st, True)
    ms = {name: st.vm.auto.status()["ms"] for name, st in states}
    lead = max(ms.values())
    for name, st in states:
        if ms[name] < lead:
            st.vm.auto.step(lead - ms[name])
    return {name: st.vm.auto.status()["ms"] for name, st in states}


@tool()
def vm_list() -> list[dict]:
    """The VMs this server knows: name, group, folder, 86Box binary, running, held, emulated time.
    The one marked current is the one tools act on without a vm argument."""
    _save_current()
    out = []
    for name, st in _vms.items():
        v = st.vm
        if v is None:
            continue                    # a name that never had a VM
        running = v.running()
        out.append({"name": name, "group": _group_of(name), "current": name == _current, "running": running,
                    "vm_dir": str(v.vm_dir) if v.vm_dir else None,
                    "binary": str(v.binary) if hasattr(v, "binary") else None,
                    "held": st.held if running else None,
                    "ms": v.auto.status()["ms"] if running else None})
    return out


@tool()
def step_all(ms: int, quantum_ms: int | None = None, group: str | None = None) -> str:
    """Run every running VM of a group ms of emulated time in lockstep, for multiplayer between VMs.

    group: by default the current VM's ("default" unless vm_group or vm_start put it in another).
    All of them are held first, and any behind the furthest along are stepped up to it. Then they all
    run quantum_ms (default the group quantum, 10 ms) at a time, in parallel, waiting for every VM
    after each quantum, so their clocks never drift apart by more than one quantum. Packets between
    them (86Box's network switch) travel in host time, so an exchange lands within a quantum of when
    it was sent: close, but not bit-for-bit repeatable the way one held VM is. Smaller quanta are
    closer and slower. Other tools step a group together too; this is the one that needs no input.
    """
    quantum_ms = quantum_ms or _quantum_ms
    if ms <= 0 or quantum_ms <= 0:
        raise ValueError("ms and quantum_ms must be positive")
    states = _states_or_fail(group)
    t0 = time.monotonic()
    start = _align(states)
    _run_in_lockstep(states, ms, quantum_ms)
    end = {name: st.vm.auto.status()["ms"] for name, st in states}
    wall = time.monotonic() - t0
    times = ", ".join(f"{n} t={end[n]}ms" for n in end)
    return (f"{len(states)} VMs ran {ms} ms in lockstep ({quantum_ms} ms quanta), in {wall:.1f} s wall; "
            f"started at t={max(start.values())}ms; now {times}")


def _states_or_fail(group: str | None) -> list[tuple[str, _VMState]]:
    states = _group_states(group)
    if not states:
        raise AutomationError(f"no VM running in group {group or _group_of(_current)!r}; call vm_start first")
    return states


def _run_in_lockstep(states, ms: int, quantum_ms: int) -> None:
    from concurrent.futures import ThreadPoolExecutor

    rounds, rest = divmod(ms, quantum_ms)
    with ThreadPoolExecutor(len(states)) as pool:
        for q in [quantum_ms] * rounds + ([rest] if rest else []):
            list(pool.map(lambda nst: nst[1].vm.auto.step(q), states))
            _pump_serial(states)


class _SerialEnd:
    """One end of a serial link: a VM's COM port, and how far its output has been passed on."""

    def __init__(self, vm_name: str, port: int, wiring: str = "full"):
        self.vm_name, self.port, self.next, self.carry = vm_name, port, 0, b""
        self.wiring = wiring
        self.sent = 0
        self.lost = 0


# Null-modem cables between VMs' COM ports (each with serialN_device = automation).
_serial_links: list[tuple[_SerialEnd, _SerialEnd]] = []


def _has_serial(name: str) -> bool:
    """Whether a VM's COM ports have something at the other end that needs pumping."""
    return any(n == name for n, _ in _serial_peers) or any(e.vm_name == name for link in _serial_links for e in link)


def _pump_serial(states) -> None:
    """Between quanta: move bytes over the serial links and to and from the host-side peers."""
    if _serial_peers:
        _pump_serial_peers(states)
    if _serial_links:
        _pump_serial_links(states)


def _pump_serial_links(states) -> None:
    """Each end's new output becomes the other end's input, and each end's DTR and RTS become the
    other's DSR/DCD and CTS, as a null-modem cable wires them. Both VMs are held here, so where the
    bytes land in emulated time doesn't depend on the host."""
    running = {n: st for n, st in states}
    for a, b in _serial_links:
        if a.vm_name not in running or b.vm_name not in running:
            continue
        auto = {a: running[a.vm_name].vm.auto, b: running[b.vm_name].vm.auto}
        out = {}
        for src in (a, b):
            r = auto[src].serial_out(src.port, src.next)
            src.next, src.lost = r["next"], src.lost + r["lost"]
            out[src] = r
        for src, dst in ((a, b), (b, a)):
            data = src.carry + out[src]["data"]
            src.carry = b""
            if data:
                taken = auto[dst].serial_in(dst.port, data)
                src.sent += taken
                src.carry = data[taken:]        # the peer's UART is behind: the rest goes next quantum
            _set_link_lines(auto[dst], dst, out[src], out[dst])


# How a cable drives a port's CTS, DSR and DCD, from the far end's and its own DTR and RTS.
_WIRINGS = ("full", "loopback", "none")


def _set_link_lines(auto, end: _SerialEnd, far: dict, own: dict) -> None:
    if end.wiring == "full":
        lines = far["rts"], far["dtr"], far["dtr"]
    elif end.wiring == "loopback":
        lines = own["rts"], own["dtr"], own["dtr"]
    else:
        lines = 1, 1, 1
    auto.serial_lines(end.port, cts=lines[0], dsr=lines[1], dcd=lines[2])


@tool()
def serial_link(vm_a: str, vm_b: str, port_a: int = 1, port_b: int = 1, wiring: str = "full",
                unlink: bool = False) -> str:
    """Join two VMs' COM ports with a null-modem cable, for serial multiplayer and file transfer
    between guests. unlink=True removes it.

    Both ports need the automation backend (serialN_device = automation in each 86box.cfg), and both
    VMs must be held in one lockstep group (see vm_group): bytes cross after every lockstep quantum,
    so a quantum of 10 ms delays each byte by up to 10 ms. The guests' baud rates are their own
    business: bytes reach each guest at the rate its UART is set to.

    wiring, the handshake lines as null-modem cables wire them:
    - "full": each side's RTS drives the other's CTS, and DTR the other's DSR and DCD. Software
      that raises them (most comms programs and games) needs this.
    - "loopback": each side's own RTS drives its CTS, and DTR its DSR and DCD. The BIOS serial
      routines (INT 14h, so DOS's COPY to and from COM1) raise only DTR while receiving, which
      stalls the sender on a full cable.
    - "none": CTS, DSR and DCD always on.
    """
    for name in (vm_a, vm_b):
        st = _vms.get(name)
        if st is None or st.vm is None or not st.vm.running():
            raise AutomationError(f"no running VM called {name!r}")
    key = {(vm_a, port_a), (vm_b, port_b)}
    for link in list(_serial_links):
        if {(e.vm_name, e.port) for e in link} == key:
            _serial_links.remove(link)
            if unlink:
                return f"unlinked {vm_a} COM{port_a} and {vm_b} COM{port_b}"
    if unlink:
        raise ValueError(f"no link between {vm_a} COM{port_a} and {vm_b} COM{port_b}")
    if wiring not in _WIRINGS:
        raise ValueError(f"wiring must be one of {', '.join(_WIRINGS)}")
    if _group_of(vm_a) != _group_of(vm_b):
        raise ValueError(f"{vm_a} and {vm_b} are in different groups; put them in one with vm_group")
    for name, port in ((vm_a, port_a), (vm_b, port_b)):
        if any((e.vm_name, e.port) == (name, port) for link in _serial_links for e in link):
            raise ValueError(f"{name} COM{port} is already linked")
    ends = (_SerialEnd(vm_a, port_a, wiring), _SerialEnd(vm_b, port_b, wiring))
    state = {}
    for end in ends:
        # From now on: what the guest sent before the cable was plugged in stays on its side.
        state[end] = _vms[end.vm_name].vm.auto.serial_out(end.port, 0, 1)
        end.next = state[end]["pos"]
    for end, far in (ends, ends[::-1]):
        _set_link_lines(_vms[end.vm_name].vm.auto, end, state[far], state[end])
    _serial_links.append(ends)
    return f"linked {vm_a} COM{port_a} and {vm_b} COM{port_b} (null modem, {wiring} handshake)"


@tool()
def serial_links() -> list[dict]:
    """The serial links: each end's VM, port, bytes passed to the other end, and bytes lost because
    the guest sent more than 64 KiB within one quantum."""
    return [{"a": f"{a.vm_name} COM{a.port}", "b": f"{b.vm_name} COM{b.port}", "wiring": a.wiring,
             "a_to_b": a.sent, "b_to_a": b.sent, "lost": a.lost + b.lost,
             "waiting": len(a.carry) + len(b.carry)} for a, b in _serial_links]


class _PeerEnd:
    """A host-side peer on one VM's COM port, and how far the guest's output has gone to it."""

    def __init__(self, peer: serialpeers.Peer, start: int):
        self.peer, self.next, self.carry = peer, start, b""
        self.to_peer = self.to_guest = self.lost = 0
        self.lines: tuple | None = None


_serial_peers: dict[tuple[str, int], _PeerEnd] = {}    # (VM name, port) -> its peer


def _pump_serial_peers(states) -> None:
    running = {n: st for n, st in states}
    for (name, port), end in list(_serial_peers.items()):
        st = running.get(name)
        if st is None:
            continue
        auto = st.vm.auto
        r = auto.serial_out(port, end.next)
        end.next, end.lost = r["next"], end.lost + r["lost"]
        end.to_peer += len(r["data"])
        peer = end.peer
        if getattr(peer, "pending_dial", None) is not None:
            # CONNECT reports the guest UART's speed, as a modem would report the line's.
            peer.baud = next((p.get("baud") for p in auto.info().get("serial", []) if p["port"] == port), None)
        peer.from_guest(r["data"], r["ms"], bool(r["dtr"]), bool(r["rts"]))
        data = end.carry + peer.to_guest(r["ms"])
        if data:
            taken = auto.serial_in(port, data)
            end.to_guest += taken
            end.carry = data[taken:]
        lines = peer.lines().as_tuple()
        if lines != end.lines:
            auto.serial_lines(port, *lines)
            end.lines = lines


def _detach_serial(name: str, port: int | None = None) -> list[str]:
    """Remove a VM's peers and links (on one port, or all); returns what was removed."""
    gone = []
    for (n, p), end in list(_serial_peers.items()):
        if n == name and port in (None, p):
            end.peer.close()
            del _serial_peers[(n, p)]
            gone.append(f"{end.peer.kind} on {n} COM{p}")
    for link in list(_serial_links):
        if any(e.vm_name == name and port in (None, e.port) for e in link):
            _serial_links.remove(link)
            gone.append(f"link {link[0].vm_name} COM{link[0].port} - {link[1].vm_name} COM{link[1].port}")
    return gone


def _serial_port(port: int) -> int:
    v = vm()
    if port not in v.serial_ports:
        raise ValueError(f"COM{port} isn't on the automation socket: start the VM with serial=[{port}]")
    return port


@tool()
def serial_attach(port: int = 1, peer: str = "modem", dial: dict[str, str] | None = None,
                  default: str | None = None, address: str = "127.0.0.1:0", link: str | None = None,
                  detach: bool = False) -> str:
    """Put a device on the host side of one of the VM's COM ports (started with vm_start(serial=[port])).

    peer:
    - "modem": a Hayes-compatible modem whose phone line is TCP. A terminal program in the guest
      (Telix, Procomm, Terminate) dials it as usual: ATDT 5551234 connects to the "host:port" that
      dial maps the number to ({"5551234": "127.0.0.1:2323"}), or to default for any other number;
      ATDT 127.0.0.1:2323 dials an address directly. It answers AT commands (Z, E, V, Q, H, O, S,
      &F, &C, &D, I; others are accepted), reports CONNECT at the guest's speed, raises DCD while a
      call is up, and hangs up on ATH, on +++ then ATH, when the guest drops DTR, or with NO CARRIER
      when the far end closes the connection.
    - "tcp": listens on address (port 0: any free one); the client that connects gets the guest's
      port raw, with DCD and DSR up while connected, as the far end of a null-modem cable.
    - "pty": a pseudo-terminal (POSIX) a host program opens, e.g. `minicom -D <path>`; link makes a
      symlink to it at that path.
    detach=True removes whatever is on the port (a peer or a serial_link).

    Bytes move every lockstep quantum (10 ms of emulated time) while the VM runs, held or not; a VM
    with a peer steps in quanta so traffic flows during step and the wait tools too. The far end runs
    on the host's clock, so these sessions aren't reproducible run to run the way a held VM alone is.
    """
    name = _current
    if detach:
        gone = _detach_serial(name, port)
        return "removed " + ", ".join(gone) if gone else f"nothing on {name} COM{port}"
    _serial_port(port)
    if (name, port) in _serial_peers or any((e.vm_name, e.port) == (name, port) for l in _serial_links for e in l):
        raise ValueError(f"{name} COM{port} already has something attached; detach=True first")
    if peer == "modem":
        p = serialpeers.Modem(dial=dial, default=default)
    elif peer == "tcp":
        host, _, prt = address.rpartition(":")
        p = serialpeers.TcpPeer(host or "127.0.0.1", int(prt or 0))
    elif peer == "pty":
        if os.name == "nt":
            raise ValueError("pty needs a POSIX host; use peer='tcp' on Windows")
        p = serialpeers.PtyPeer(link)
    else:
        raise ValueError("peer must be modem, tcp or pty")
    v = vm()
    _serial_peers[(name, port)] = _PeerEnd(p, v.auto.serial_out(port, 0, 1)["pos"])
    v.auto.serial_lines(port, *p.lines().as_tuple())
    _ensure_serial_pump_thread()
    d = p.describe()
    where = {"modem": lambda: f"dialling {default or 'nothing by default'}" + (f", numbers {dial}" if dial else ""),
             "tcp": lambda: f"listening on {d['address']}",
             "pty": lambda: f"at {d['path']}" + (f" (link {link})" if link else "")}[peer]()
    return f"{peer} on {name} COM{port}, {where}"


@tool()
def serial_status() -> list[dict]:
    """Every COM port with something on its host side: the peer (modem state and recent events,
    pty path, TCP address and client) and the bytes moved each way."""
    out = []
    for (name, port), end in _serial_peers.items():
        out.append({"vm": name, "port": port, **end.peer.describe(), "to_peer": end.to_peer,
                    "to_guest": end.to_guest, "lost": end.lost, "waiting": len(end.carry)})
    return out + [{"link": l["a"] + " - " + l["b"], **l} for l in serial_links()]


_pump_thread: threading.Thread | None = None


def _ensure_serial_pump_thread() -> None:
    """Released (real-time) VMs don't step through the server, so a thread pumps their peers and
    links every 10 ms of host time. Held VMs are pumped as they step."""
    global _pump_thread
    if _pump_thread and _pump_thread.is_alive():
        return

    def run():
        while _serial_peers or _serial_links:
            time.sleep(0.01)
            if not _lock.acquire(timeout=0.05):
                continue                 # a tool call is running; it pumps if it steps
            try:
                _save_current()
                free = [(n, st) for n, st in _running_states() if not st.held and _has_serial(n)]
                if free:
                    _pump_serial(free)
            except Exception:
                pass
            finally:
                _lock.release()

    _pump_thread = threading.Thread(target=run, name="serial-pump", daemon=True)
    _pump_thread.start()


@tool()
def vm_group(group: str | None = None, quantum_ms: int | None = None) -> str:
    """Put the current VM in a group (group=None just reports), or set the lockstep quantum.

    Held VMs in one group step together: any tool that lets held time pass on one of them (step,
    mouse, mouse_to, press_keys, wait_for_text...) steps them all, quantum_ms at a time, so a
    multiplayer game sees its peers keep up rather than dropping one that froze while another got
    input. Every VM starts in "default", so by default they all step together. A VM in a group of its
    own runs independently: its time passes only in its own tool calls. Joining a group brings the
    VM to the group's emulated time. quantum_ms (default 10) applies to every group.
    """
    global _quantum_ms
    if quantum_ms is not None:
        if quantum_ms <= 0:
            raise ValueError("quantum_ms must be positive")
        _quantum_ms = quantum_ms
    if group is not None:
        _groups[_current] = group
        held = [(n, st) for n, st in _group_states(group) if st.held]
        if len(held) > 1:
            _align(held)
    g = _group_of(_current)
    members = [n for n, _ in _group_states(g)]
    return f"{_current} is in group {g!r} with {', '.join(members) or 'no running VMs'}; quantum {_quantum_ms} ms"


@tool()
def hold_all(group: str | None = None) -> str:
    """Hold every running VM of a group (default: the current VM's), at the same emulated time."""
    states = _states_or_fail(group)
    at = _align(states)
    return f"held {len(states)} VMs at t={max(at.values())}ms"


@tool()
def release_all(align: bool = True, group: str | None = None) -> str:
    """Let every running VM of a group (default: the current VM's) run in real time, released
    together. With align (default), they are first brought to the same emulated time, so they start
    out level; each then paces itself to real time, and measure_speed(vm=...) shows one that falls
    behind."""
    from concurrent.futures import ThreadPoolExecutor

    states = _states_or_fail(group)
    at = _align(states) if align else {}
    for _, st in states:
        st.vm.let_cpu_run()
    with ThreadPoolExecutor(len(states)) as pool:
        list(pool.map(lambda nst: nst[1].vm.auto.release(), states))
    for name, st in states:
        _set_held(name, st, False)
    return f"released {len(states)} VMs" + (f" from t={max(at.values())}ms" if at else "")


@tool()
def measure_speed(seconds: float = 10, paced: bool = True) -> str:
    """How fast 86Box emulates this machine on this host: emulated seconds per wall second, in %.

    paced=True runs the machine in real time for `seconds` of wall time, as a person would use it:
    100 % means 86Box keeps up, and it never reads above 100 (86Box waits for real time).
    paced=False steps `seconds` of emulated time as fast as the host allows: the throughput, uncapped.
    That is the number to compare 86Box builds or settings by (vm_start(binary=...) for each, the same
    VM and workload). The machine is left held or running, as it was. The result depends on what the
    guest is doing at the time and on host load, so measure the same point of the same workload, a
    few times.
    """
    if not 0 < seconds <= 600:
        raise ValueError("seconds must be in (0, 600]")
    v = vm()
    was_held = _held
    if paced:
        v.let_cpu_run()
        v.auto.release()
        ms0, t0 = v.auto.status()["ms"], time.monotonic()
        time.sleep(seconds)
        ms1, t1 = v.auto.status()["ms"], time.monotonic()
        if was_held:
            v.auto.hold()
    else:
        if not was_held:
            v.auto.hold()
        ms0, t0 = v.auto.status()["ms"], time.monotonic()
        ms1 = v.auto.step(int(seconds * 1000))["ms"]
        t1 = time.monotonic()
        if not was_held:
            v.auto.release()
    emulated, wall = (ms1 - ms0) / 1000, t1 - t0
    pct = 100 * emulated / wall if wall else 0.0
    own = v.auto.info().get("speed_percent")
    return (f"{pct:.0f} % of real time, {'paced' if paced else 'unpaced'}: {emulated:.1f} s emulated in "
            f"{wall:.1f} s wall. 86Box's own speed_percent: {own}. "
            f"Machine left {'held' if was_held else 'running in real time'}.")


@tool()
def hold() -> str:
    """Freeze emulated time between tool calls (turn-based mode)."""
    global _held
    _held = True
    return _time_note(vm().auto.hold())


@tool()
def release() -> str:
    """Let the machine run in real time between tool calls."""
    global _held
    _held = False
    v = vm()
    v.let_cpu_run()
    return _time_note(v.auto.release())


@tool()
def fast_forward(on: bool | None = None) -> str:
    """Turn 86Box's fast forward on or off (on=None just reports it), as its menu does.

    With it on, a released machine runs as fast as the host allows instead of in real time: good for
    getting through a slow install or boot. A held machine is unaffected; its steps are already unpaced.
    Released, tools that wait (press_keys, type_text) wait in host time, so more emulated time passes.
    """
    r = vm().auto.fast_forward(on)
    return f"fast forward {'on' if r.get('ff') else 'off'}; {_time_note(r)}"


@tool()
def step(ms: int) -> str:
    """Run exactly ms milliseconds of emulated time (as fast as the host allows), then hold again."""
    return _time_note(_step(ms))


@tool()
def reset(kind: str = "hard") -> str:
    """Reset the machine: kind='hard' (power cycle) or 'cad' (Ctrl+Alt+Del)."""
    if kind not in ("hard", "cad"):
        raise ValueError("kind must be 'hard' or 'cad'")
    return _time_note(vm().auto.call(f"reset {kind}"))


# Screen --------------------------------------------------------------------


@tool()
def screenshot(max_width: int = 640) -> list:
    """The current frame as an image, scaled down to max_width pixels wide (0 = native size)."""
    png, info = _snapshot(max_width)
    return [Image(data=png, format="png"), f"{info['w']}x{info['h']} native, {_time_note(info)}"]


@tool()
def screen_text() -> str:
    """The text-mode screen as plain text (CP437 decoded), with video mode and cursor position."""
    return vm().auto.text().render()


# Input ---------------------------------------------------------------------


@tool()
def press_keys(keys: list[str], hold_ms: int = 60, gap_ms: int = 60) -> str:
    """Press keys or combos in sequence, e.g. ["enter"], ["ctrl+c"], ["up","up","space"], ["alt+f","x"].

    Names: letters, digits, f1-f12, esc, enter, space, tab, backspace, up/down/left/right, home/end,
    pageup/pagedown, insert/delete, ctrl/alt/shift (and l/r variants), kp0-kp9, or hex scancodes like 0x1c.
    """
    v = vm()
    for combo in keys:
        codes = K.parse_combo(combo)
        v.auto.keys(True, codes)
        _advance(hold_ms)
        v.auto.keys(False, list(reversed(codes)))
        _advance(gap_ms)
    return f"pressed {len(keys)} key(s); {_time_note(v.auto.status())}"


@tool()
def together(actions: list[dict], hold_ms: int = 60, gap_ms: int = 60) -> str:
    """Give several VMs input at the same emulated time, e.g. every player's order on one game tick.

    actions: one per VM, each {"vm": name} plus one of
      "keys": ["esc", "ctrl+c", ...]   key combos, pressed one after another
      "text": "Alpha\n"                typed
      "tablet": [x, y] or [x, y, buttons]   a touch at that pixel (see tablet)
      "mouse": [dx, dy] or [dx, dy, buttons]   relative motion, with buttons
      "pointer": [x, y]                  the pointer to that pixel, through MOUSEBOX (see mouse_to)
    In each round, every VM gets its next press at once (a key or character down, the finger down,
    the mouse moved and its buttons down); then the VMs run hold_ms together, everything is
    released, and they run gap_ms. Keys and text go one per round, so the k-th key reaches every VM
    at the same time. The VMs must all be held and in one group (see vm_group).
    """
    by_vm = {}
    for a in actions:
        name = a.get("vm")
        kinds = [k for k in ("keys", "text", "tablet", "mouse", "pointer") if k in a]
        if name is None or len(kinds) != 1:
            raise ValueError(f"each action needs a vm and exactly one of keys, text, tablet, mouse, pointer: {a}")
        if name in by_vm:
            raise ValueError(f"two actions for {name}")
        kind = kinds[0]
        if kind == "keys":
            seq = [("keys", K.parse_combo(c)) for c in a["keys"]]
        elif kind == "text":
            seq = []
            for ch in a["text"]:
                code, shifted = K.char_keys(ch)
                seq.append(("keys", ([K.scancode("shift")] if shifted else []) + [code]))
        elif kind == "pointer":
            seq = [("pointer", [int(n) for n in a["pointer"][:2]])]
        else:
            v = list(a[kind]) + ([1] if kind == "tablet" else [0]) * (3 - len(a[kind]))
            seq = [(kind, [int(n) for n in v[:3]])]
        by_vm[name] = seq
    _save_current()
    for name in by_vm:
        st = _vms.get(name)
        if st is None or st.vm is None or not st.vm.running():
            raise AutomationError(f"no VM running called {name!r}")
        if not st.held:
            raise ValueError(f"{name} isn't held: together needs held VMs (hold_all)")
    groups = {_group_of(n) for n in by_vm}
    if len(groups) != 1:
        raise ValueError(f"the VMs are in different groups: {sorted(groups)}")
    states = [(n, st) for n, st in _group_states(groups.pop()) if st.held]
    _align(states)
    rounds = max(len(q) for q in by_vm.values())
    if any(q[0][0] == "pointer" for q in by_vm.values()):
        hold_ms = max(hold_ms, 120)               # MOUSEBOX takes moves on a timer tick (55 ms)
    for r in range(rounds):
        now = [(n, _vms[n].vm.auto, q[r]) for n, q in by_vm.items() if r < len(q)]
        waits = {}                                # MOUSEBOX moves to see acknowledged
        for name, auto, (kind, arg) in now:            # press
            if kind == "keys":
                auto.keys(True, arg)
            elif kind == "tablet":
                auto.call(f"tablet {arg[0]} {arg[1]} {arg[2]}")
            elif kind == "pointer":
                vmobj = _vms[name].vm
                addr = _mousebox(vmobj)
                if addr is None:
                    raise ValueError(f"MOUSEBOX isn't loaded in {name}")
                waits[name] = (vmobj, addr, _mousebox_send(vmobj, addr, *_mousebox_units(vmobj, addr, arg[0], arg[1])))
            else:
                auto.mouse(arg[0], arg[1], arg[2])
        _align(states)                                    # key events take a little time each
        _run_in_lockstep(states, max(hold_ms, 1), _quantum_ms)
        for _ in range(20):                               # MOUSEBOX takes a move on a timer tick,
            waits = {n: w for n, w in waits.items()       # later if the program was in the driver
                     if _mousebox_read(w[0], w[1])["ack"] != w[2]}
            if not waits:
                break
            _run_in_lockstep(states, 30, _quantum_ms)
        if waits:
            raise AutomationError(f"MOUSEBOX didn't take the move in {', '.join(waits)} within 600 ms")
        for name, auto, (kind, arg) in now:            # release
            if kind == "keys":
                auto.keys(False, list(reversed(arg)))
            elif kind == "tablet":
                auto.call(f"tablet {arg[0]} {arg[1]} 0")
                auto.call("tablet out")
            elif kind == "pointer":
                pass
            elif arg[2]:
                auto.mouse(0, 0, 0)
        _align(states)
        if gap_ms > 0:
            _run_in_lockstep(states, gap_ms, _quantum_ms)
    end = {n: st.vm.auto.status()["ms"] for n, st in states}
    return (f"{len(by_vm)} VMs got their input together in {rounds} round(s); "
            f"now t={max(end.values())}ms on all {len(states)} VMs of the group")


def _type(text: str, ms_per_key: int) -> None:
    v = vm()
    shift = K.scancode("shift")
    for ch in text:
        code, shifted = K.char_keys(ch)
        if shifted:
            v.auto.keys(True, [shift])
        v.auto.keys(True, [code])
        _advance(ms_per_key // 2)
        v.auto.keys(False, [code])
        if shifted:
            v.auto.keys(False, [shift])
        _advance(ms_per_key - ms_per_key // 2)


@tool()
def type_text(text: str, ms_per_key: int = 50) -> str:
    """Type text on the emulated keyboard (US layout). Use \\n for Enter."""
    _type(text, ms_per_key)
    return f"typed {len(text)} chars; {_time_note(vm().auto.status())}"


@tool()
def mouse(dx: int = 0, dy: int = 0, left: bool = False, right: bool = False, middle: bool = False,
          hold_ms: int = 30, release: bool = True) -> str:
    """Move the mouse by (dx, dy) and set buttons, run hold_ms, then release the buttons.
    release=False leaves the buttons down (held in place, e.g. a dropdown that picks on release, or
    a fire button); the next mouse call sets them again, so call with no buttons to let go. For a
    drag between two points, use mouse_drag."""
    v = vm()
    buttons = (1 if left else 0) | (2 if right else 0) | (4 if middle else 0)
    v.auto.mouse(dx, dy, buttons)
    _advance(hold_ms)
    if buttons and release:
        v.auto.mouse(0, 0, 0)
        _advance(hold_ms)
    return _time_note(v.auto.status())


@tool()
def tablet(x: int, y: int, buttons: int = 1, hold_ms: int = 30, release: bool = True) -> str:
    """Touch the screen, or put a tablet's pen, at pixel (x, y) of the frame (screenshot coordinates):
    absolute input, for a VM with a touch screen, tablet or light pen configured (86box.cfg
    [Input devices] tablet_type, e.g. microtouch_touchpen on COM1) and its guest driver loaded.

    buttons: 1 = touching (the pen's tip), 0 = hovering, other bits for a pen's side buttons.
    Runs hold_ms, then lifts the finger or pen away, unless release=False, which leaves it there
    (the next tablet call moves it: drag by calling again with release=False, then once more to
    let go). The relative mouse keeps working alongside: mouse() goes to the mouse, tablet() to the
    tablet. Needs 86Box with the automation "tablet" command.
    """
    v = vm()
    v.auto.call(f"tablet {int(x)} {int(y)} {int(buttons)}")
    _advance(hold_ms)
    if release:
        if buttons:
            v.auto.call(f"tablet {int(x)} {int(y)} 0")
            _advance(hold_ms)
        v.auto.call("tablet out")
    return _time_note(v.auto.status())


def _frame() -> PILImage.Image:
    img = PILImage.open(io.BytesIO(vm().auto.screenshot()["png"]))
    img.load()
    return img.convert("RGB")


def _changes(a: PILImage.Image, b: PILImage.Image) -> PILImage.Image:
    from PIL import ImageChops

    return ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)


def _pointer_from_screen(frame: PILImage.Image, home: PILImage.Image, corner: int, hotspot: str,
                         noise: PILImage.Image | None = None):
    """Where the pointer is: the changed region versus the pointer-at-home frame, ignoring the home
    corner and anything that changes by itself (noise: e.g. a blinking caret)."""
    from PIL import ImageChops, ImageDraw

    if frame.size != home.size:
        return None
    diff = _changes(frame, home)
    if noise is not None and noise.size == diff.size:
        diff = ImageChops.subtract(diff, noise)
    ImageDraw.Draw(diff).rectangle([0, 0, corner, corner], fill=0)
    if not diff.getbbox():
        return None
    # Other things change too (a blinking caret, a clock): take the densest pointer-sized area,
    # not the bounding box of every change.
    cell = 16
    small = diff.resize((max(1, diff.width // cell), max(1, diff.height // cell)), PILImage.Resampling.BOX)
    w = small.width
    data = list(small.tobytes())
    best = max(range(len(data)), key=lambda i: sum(
        data[j] for j in (i - 1, i, i + 1, i - w, i + w) if 0 <= j < len(data) and abs(j % w - i % w) <= 1))
    cx, cy = (best % w) * cell, (best // w) * cell
    box = diff.crop((max(0, cx - 32), max(0, cy - 32), cx + cell + 32, cy + cell + 32)).getbbox()
    if not box:
        return None
    box = (box[0] + max(0, cx - 32), box[1] + max(0, cy - 32), box[2] + max(0, cx - 32), box[3] + max(0, cy - 32))
    if hotspot == "center":
        return (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    return box[0], box[1]


_mouse_state: dict = {}


# Slow moves: at most this many counts per ~10 ms (one PS/2 packet at the default 100 Hz). Guest
# drivers accelerate fast motion (Windows 3.x doubles it above 2 counts per report), which makes the
# pointer gain depend on speed; below the threshold the gain is constant and measurable.
SLOW_COUNTS = 2


def _move_counts(dx: int, dy: int, buttons: int, slow: bool = False) -> None:
    """Relative move, holding the given buttons: packet-sized chunks, or unaccelerated slow chunks."""
    v = vm()
    per, wait = (SLOW_COUNTS, 10) if slow else (250, 25)
    n = max(1, (max(abs(dx), abs(dy)) + per - 1) // per)
    sent_x = sent_y = 0
    for i in range(1, n + 1):
        tx, ty = round(dx * i / n), round(dy * i / n)
        v.auto.mouse(tx - sent_x, ty - sent_y, buttons)
        sent_x, sent_y = tx, ty
        _advance(wait)


@tool()
def mouse_drag(x1: int, y1: int, x2: int, y2: int, pointer_addr: int | None = None,
               hotspot: str = "top-left") -> str:
    """Drag with the left button from (x1, y1) to (x2, y2), e.g. a card, a window or an icon.

    Places the pointer on (x1, y1) with mouse_to, presses, moves to (x2, y2) in a few steps using the
    pointer gain mouse_to just measured (the pointer can't be tracked while it carries something),
    then releases. Check the result with a screenshot.

    If drags overshoot or the reported gain varies between calls, the guest is accelerating the
    pointer: in Windows 3.x set Control Panel > Mouse > Mouse Tracking Speed to Slow first.
    """
    v = vm()
    placed = mouse_to(x1, y1, pointer_addr=pointer_addr, hotspot=hotspot, tolerance=2)
    if not placed.startswith("at target"):
        return f"could not grab: {placed}"
    gx, gy = _mouse_state["gain"]
    v.auto.mouse(0, 0, 1)
    _advance(60)
    # Same slow, unaccelerated motion mouse_to measured its gain with.
    _move_counts(round((x2 - x1) / gx), round((y2 - y1) / gy), 1, slow=True)
    _advance(80)
    v.auto.mouse(0, 0, 0)
    _advance(100)
    return f"dragged from ({x1},{y1}) to about ({x2},{y2}); gain {gx:.2f}x{gy:.2f} px/count"


MOUSEBOX_SIG = b"MOUSEBOX"
_MB_FIELDS = ("seq", "ack", "x", "y", "xmin", "xmax", "ymin", "ymax", "xfull", "yfull",
              "dx", "dy", "buttons", "hmask")


def _mousebox(v) -> int | None:
    """Where MOUSEBOX's mailbox is in this VM (examples/touch), or None. Cached; checked on use."""
    addr = getattr(v, "_mousebox_addr", None)
    if addr is not None and v.auto.memr(addr - 3, 11) == b"\xe9" + v.auto.memr(addr - 2, 2) + MOUSEBOX_SIG:
        return addr
    data = b"".join(v.auto.memr(a, min(CHUNK, 0xA0000 - a)) for a in range(0, 0xA0000, CHUNK))
    v._mousebox_addr = None
    i = data.find(MOUSEBOX_SIG)
    while i >= 0:
        # The resident copy: a .COM's code starts at offset 100h of its segment with a near jump,
        # and the signature follows it. The name also turns up in the environment, AUTOEXEC.BAT's
        # buffers and messages, which must not be written to.
        if i >= 3 and (i - 0x103) % 16 == 0 and data[i - 3] == 0xE9:
            v._mousebox_addr = i
            break
        i = data.find(MOUSEBOX_SIG, i + 1)
    return v._mousebox_addr


def _mousebox_read(v, addr: int) -> dict:
    import struct

    raw = v.auto.memr(addr + 8, 40 + 128)
    words = struct.unpack("<14H", raw[:28])
    st = dict(zip(_MB_FIELDS, words))
    for k in ("x", "y", "xmin", "xmax", "ymin", "ymax", "xfull", "yfull", "dx", "dy"):
        st[k] = st[k] - 65536 if st[k] >= 32768 else st[k]
    st["handler"] = struct.unpack("<I", raw[28:32])[0]
    calls = struct.unpack("<64H", raw[32:160])
    st["calls"] = {f"{n:02X}h": c for n, c in enumerate(calls) if c}
    return st


def _mousebox_units(v, addr: int, x: int, y: int) -> tuple[int, int]:
    """Frame pixel -> the driver's units, over the widest range the program set (the screen)."""
    st = _mousebox_read(v, addr)
    status = v.auto.status()
    w, h = status.get("w") or 640, status.get("h") or 480
    return (int((x + 0.5) * (st["xfull"] + 1) / w), int((y + 0.5) * (st["yfull"] + 1) / h))


def _mousebox_send(v, addr: int, ux: int, uy: int) -> int:
    import struct

    seq = (_mousebox_read(v, addr)["seq"] + 1) & 0xFFFF
    v.auto.memw(addr + 12, struct.pack("<hh", ux, uy))
    v.auto.memw(addr + 8, struct.pack("<H", seq))
    return seq


@tool()
def mouse_state() -> dict:
    """How the program in a DOS VM reads the mouse, from MOUSEBOX (examples/touch) if it is loaded:
    the range it set (functions 7, 8), the widest range since its reset (the whole screen, in the
    driver's units), the driver's pointer and buttons, whether it has an event handler, and which
    INT 33h functions it has called how often. A program calling 0Ch (an event handler) may draw its
    own pointer from mickeys; 0Bh means it reads motion counters (mouselook: use relative mouse)."""
    v = vm()
    addr = _mousebox(v)
    if addr is None:
        raise ValueError("MOUSEBOX isn't loaded in this VM (examples/touch/MOUSEBOX.COM, after a mouse driver)")
    st = _mousebox_read(v, addr)
    st["address"] = addr
    return st


@tool()
def mouse_to(x: int, y: int, pointer_addr: int | None = None, hotspot: str = "top-left",
             tolerance: int = 1, max_moves: int = 10, use_mousebox: bool = True) -> str:
    """Put the pointer at (x, y), closing the loop on where it actually lands.

    Guest mouse drivers scale and accelerate relative motion, so this homes the pointer to the top-left
    corner, moves, measures, and corrects. Two ways to measure:
    - pointer_addr: physical address of the program's pointer position, two little-endian int16 (x, y).
      Exact; x/y are then in the program's own units.
    - otherwise the screen: the pointer is found by comparing with a frame taken while it sits in the
      corner, so the rest of the screen must be still (menus, installers, desktops). x/y are native
      frame pixels (screenshot reports the native size). hotspot: "top-left" for arrow pointers,
      "center" for crosshairs. Targets within ~32 px of the top-left corner can't be measured this way.
    With MOUSEBOX loaded in a DOS guest (examples/touch, after the mouse driver) and no pointer_addr,
    neither: MOUSEBOX puts the pointer there directly, for every way programs read the mouse,
    including ones that draw their own pointer from mickeys. x/y are frame pixels. Then click with
    mouse(left=True). use_mousebox=False forces the screen method.
    """
    v = vm()
    if pointer_addr is None and use_mousebox:
        addr = _mousebox(v)
        if addr is not None:
            ux, uy = _mousebox_units(v, addr, x, y)
            seq = _mousebox_send(v, addr, ux, uy)
            for _ in range(20):
                _advance(30)
                if _mousebox_read(v, addr)["ack"] == seq:
                    _advance(30)
                    st = _mousebox_read(v, addr)
                    return (f"at target via MOUSEBOX: ({x},{y}) is ({ux},{uy}) in the driver's units; the driver "
                            f"has the pointer at ({st['dx']},{st['dy']}) (range x {st['xmin']}..{st['xmax']}, "
                            f"y {st['ymin']}..{st['ymax']})")
            raise AutomationError("MOUSEBOX didn't take the move within 600 ms of emulated time: is the guest "
                                  "running DOS code (not halted in a debugger)?")
    corner = 32

    noise = None

    def measure(home):
        if pointer_addr is not None:
            raw = v.auto.memr(pointer_addr, 4)
            return int.from_bytes(raw[:2], "little", signed=True), int.from_bytes(raw[2:], "little", signed=True)
        return _pointer_from_screen(_frame(), home, corner, hotspot, noise)

    # Home in packet-sized chunks: 86Box hands motion to the guest at most 255 counts per mouse
    # packet and queues the rest, so one huge move would still be draining (and would swallow the
    # next moves) long after it was sent.
    for _ in range(12):
        v.auto.mouse(-250, -250, 0)
        _advance(25)
    _advance(50)
    home = _frame() if pointer_addr is None else None
    if home is not None:
        # Learn what changes on its own (blinking carets, clocks) while the pointer sits still.
        from PIL import ImageFilter

        _advance(270)
        noise = _changes(_frame(), home)
        _advance(270)
        noise = PILImage.composite(noise, _changes(_frame(), home), noise).filter(ImageFilter.MaxFilter(9))
    pos = None
    if pointer_addr is None:
        # Nudge out of the corner so there is something to measure; how far one count moves the
        # pointer depends on the guest's driver, so keep pushing until it shows up.
        nudge = corner * 2
        for _ in range(5):
            v.auto.mouse(nudge, nudge, 0)
            _advance(60)
            pos = measure(home)
            if pos is not None:
                break
    else:
        pos = measure(home)
    gain_x = gain_y = 1.0
    moves = 0
    while moves < max_moves:
        if pos is None:
            return ("could not find the pointer on screen. Is it visible? Some programs hide it, e.g. dialogs "
                    "meant for the keyboard; use press_keys there. Animation elsewhere on screen can also hide it.")
        ex, ey = x - pos[0], y - pos[1]
        if abs(ex) <= tolerance and abs(ey) <= tolerance:
            break
        dx, dy = round(ex / gain_x), round(ey / gain_y)
        dx = dx or (1 if ex > 0 else -1 if ex < 0 else 0)
        dy = dy or (1 if ey > 0 else -1 if ey < 0 else 0)
        _move_counts(dx, dy, 0, slow=True)
        _advance(40)
        moves += 1
        new = measure(home)
        if new is not None:
            # Learn how far one count moves the pointer; keep the estimate sane.
            if dx and new[0] != pos[0]:
                gain_x = min(max((new[0] - pos[0]) / dx, 0.1), 10.0)
            if dy and new[1] != pos[1]:
                gain_y = min(max((new[1] - pos[1]) / dy, 0.1), 10.0)
        pos = new
    if pos is None:
        return "lost the pointer while moving"
    _mouse_state.update(pos=pos, gain=(gain_x, gain_y))
    ok = abs(x - pos[0]) <= tolerance and abs(y - pos[1]) <= tolerance
    return (f"{'at' if ok else 'NOT at'} target: pointer ({pos[0]},{pos[1]}), target ({x},{y}), "
            f"{moves} corrective moves")


@tool()
def act(hold_keys: list[str] | None = None, ms: int = 150, mouse_dx: int = 0, mouse_dy: int = 0,
        mouse_buttons: int = 0, max_width: int = 480) -> list:
    """Game-loop primitive: hold keys (and mouse) for ms of emulated time, release, return a screenshot.

    With the machine held, the game is frozen between calls, so each call is one turn. Example for DOOM:
    act(["up","ctrl"], 200) = run forward firing for 0.2 s.
    """
    v = vm()
    codes = [c for combo in (hold_keys or []) for c in K.parse_combo(combo)]
    v.auto.keys(True, codes)
    if mouse_dx or mouse_dy or mouse_buttons:
        v.auto.mouse(mouse_dx, mouse_dy, mouse_buttons)
    _advance(ms)
    v.auto.keys(False, list(reversed(codes)))
    if mouse_buttons:
        v.auto.mouse(0, 0, 0)
    png, info = _snapshot(max_width)
    return [Image(data=png, format="png"), _time_note(info)]


# Waiting and DOS -----------------------------------------------------------


@tool()
def wait_for_text(text: str, timeout_ms: int = 10000) -> str:
    """Run until text appears on one row of the text-mode screen, or timeout_ms of emulated time passes."""
    a = vm().auto
    r = _wait(lambda t: a.wait_text(text, t), timeout_ms)
    return ("found" if r.get("matched") else "TIMED OUT") + f"; {_time_note(r)}"


_PROMPT = re.compile(r"^[A-Z]:\\[^>]*>$")


@tool()
def dos_run(command: str, timeout_ms: int = 30000, poll_ms: int = 100) -> str:
    """Type a DOS command plus Enter, run until the command prompt returns, and return the screen text.

    Gives up after timeout_ms of emulated time (the program may still be running or waiting for input).
    """
    v = vm()
    _type(command + "\n", 40)
    waited = 0
    while waited < timeout_ms:
        _advance(poll_ms)
        waited += poll_ms
        scr = v.auto.text() if v.auto.status().get("bios_mode", 0) in (2, 3, 7) else None
        if scr:
            row, col = scr.cursor
            line = scr.lines[row].rstrip() if row < len(scr.lines) else ""
            if _PROMPT.match(line) and col == len(line):
                return scr.render()
    try:
        screen = v.auto.text().render()
    except AutomationError as e:
        screen = f"(no text screen: {e})"
    return f"TIMED OUT after {timeout_ms} ms emulated; still running or waiting for input.\n{screen}"


# Recording -----------------------------------------------------------------


@tool()
def record_start(directory: str, interval_ms: int = 100, audio: bool = True) -> str:
    """Record the screen: 86Box saves the displayed frame every interval_ms of emulated time into
    directory (one PNG per frame, named by emulated ms), whatever advances the clock. With audio
    (default), also every sound stream as WAVs on the same emulated clock, sample-exact for the
    sound cards, OPL and PC speaker (needs an 86Box from 2026-10-20 or later, 86Box#8420; without it,
    video only). The sound is fetched over the automation connection, so it is saved here even for
    a VM attached on another machine (vm_attach); the frames are saved on 86Box's machine.

    Use record_stop to finish; it can render a video, one video frame per recorded frame. For video
    as smooth as the guest's screen, use interval_ms 1: every frame the guest displays (at 60-70 Hz,
    about 4 MB of PNGs per second).
    """
    d = Path(directory).expanduser().resolve()
    d.mkdir(parents=True, exist_ok=True)
    v = vm()
    from . import audio as A

    # Frames and sound are kept in 86Box's memory and fetched over the automation connection, so a
    # VM on another machine (vm_attach) records here too. An 86Box without that writes the frames
    # itself, on its own machine.
    try:
        r = v.auto.call(f"record start {int(interval_ms)}")
        pulled = True
    except AutomationError:
        r = v.auto.call(f"record start {int(interval_ms)} {d}")
        pulled = False
    note = ""
    if audio or pulled:
        try:
            v.puller = A.Puller(v.auto, d, audio=audio, frames=pulled)
            if audio:
                note = " with sound" if v.puller.audio else " (no sound: this 86Box has no audio capture)"
        except AutomationError as e:
            note = f" (no sound: {e})"
    _record_dirs.append(d)
    return f"recording every {interval_ms} ms of emulated time{note} to {d}; {_time_note(r)}"


def _stop_audio(v: VM, d: Path) -> str:
    """Fetch the rest of a VM's recording (frames and sound) into d, after record stop; describe it."""
    from . import audio as A

    puller = getattr(v, "puller", None)
    if puller is None:
        return ""
    v.puller = None
    try:
        segs = puller.finish()
    except AutomationError as e:
        return f"; sound not saved: {e}"
    if puller.frames:
        lost_frames = (f", {puller.frames_lost} frames LOST (fetched too late)" if puller.frames_lost else "")
        if not puller.audio:
            return f"; {puller.frames_saved} frames fetched{lost_frames}"
    else:
        lost_frames = ""
    if not segs:
        return "; no sound" + lost_frames
    drift = [s.drift_frames for s in segs if s.drift_frames is not None]
    worst = f", worst drift {max(abs(x) for x in drift):.0f} frames" if drift else ""
    lost = sum(s.lost for s in segs)
    return ("; sound: " + ", ".join(f"{s.stream} {s.frames / s.rate:.1f} s" for s in segs) + worst +
            (f"; LOST {lost} samples (pulled too late)" if lost else "") + lost_frames)


@tool()
def record_stop(video: str | None = None, calls_log: str | None = None, fast_factor: int = 8,
                speed: list[str] | None = None, sync: bool = False) -> str:
    """Stop recording. With video (.mp4 or .gif), render it.

    With sound recorded (and no calls_log), the video plays on the emulated clock with its sound:
    real speed unless speed says otherwise ("from_ms:factor" list, as record_stop_all; sped-up
    stretches are silent), at the recording's frame rate. Otherwise real speed around interactions,
    idle stretches fast-forwarded fast_factor times; calls_log is an optional JSONL of
    {"start_ms", "end_ms", "caption"} for captions (mcpctl writes one). sync=True shows each frame's
    block, CPU cycles, sound sample position and hashes of its pixels and sound under the picture.
    """
    from . import audio as A

    v = vm()
    r = v.auto.call("record stop")
    msg = f"stopped recording; {r.get('frames')} frames"
    if _record_dirs:
        msg += _stop_audio(v, _last_record_dir())
    if video:
        frames = _last_record_dir()
        out = Path(video).expanduser()
        if A.load(frames) and not calls_log and out.suffix == ".mp4":
            from .montage import frame_period, parse_speed, render_grid

            msg += "; " + render_grid([(_current, [frames])], out, parse_speed(speed),
                                      frame_ms=frame_period([frames]), audio="first", sync=sync)
        else:
            from .ctl import render_timeline

            render_timeline(frames, out, Path(calls_log).expanduser() if calls_log else None,
                            fast_factor=fast_factor)
            msg += f"; rendered {video}"
    return msg


@tool()
def record_stop_all(video: str | None = None, speed: list[str] | None = None, labels: dict[str, str] | None = None,
                    group: str | None = None, audio: str = "first", sync: bool = False) -> str:
    """Stop recording on every VM of a group (default: the current VM's) and, with video (.mp4), render
    their recordings side by side on the shared emulated clock: two VMs next to each other, up to
    four 2x2, up to eight 4x2, each labelled, with the emulated time on top. Each VM's frames are all
    its recordings this session (record_start again to go on in a new directory), from the time all
    of them were recording to when the first stopped.

    speed: stretches of emulated time and how much faster than real time to play them, as
    "from_ms:factor", e.g. ["0:4", "75000:1"] plays boot at 4x and the rest at real speed (default
    real speed throughout; the factor shows next to the clock). labels: {vm name: caption}.
    audio (when sound was recorded): "first" VM's sound, "mix" of all, "split" (each VM panned to
    its column, so two VMs play left and right: any misalignment is audible), a VM name, or "none".
    Sound plays only in real-speed stretches. sync=True shows each frame's block, CPU cycles, sound
    sample position and hashes of its pixels and sound under each picture, and compares every VM
    with the first in the top bar: identical VMs show zero differences and matching hashes.
    """
    from .montage import frame_period, parse_speed, render_grid

    states = [(n, st) for n, st in _states_or_fail(group) if st.record_dirs]
    if not states:
        raise ValueError("no VM in the group has recorded anything this session")
    parts = []
    for n, st in states:
        frames = st.vm.auto.call("record stop").get("frames")
        parts.append(f"{n} {frames} frames" + _stop_audio(st.vm, st.record_dirs[-1]))
    msg = "stopped recording: " + ", ".join(parts)
    if video:
        sources = [((labels or {}).get(n, n), st.record_dirs) for n, st in states]
        label_audio = (labels or {}).get(audio, audio)
        frame_ms = min(frame_period(st.record_dirs) for _, st in states)
        msg += "; " + render_grid(sources, Path(video).expanduser(), parse_speed(speed), frame_ms=frame_ms,
                                  audio=label_audio, sync=sync)
    return msg


# Sound -----------------------------------------------------------------------


def _ring_mix(v: VM, ms: int, stream: str | None):
    """The last ms of every stream (or one), mixed at 48 kHz, as numpy (frames, 2)."""
    import numpy as np

    from . import audio as A

    levels = v.auto.audio_level(ms)
    names = [stream] if stream else [n for n, l in levels.items() if l["peak_db"] > -120]
    if stream and stream not in levels:
        raise AutomationError(f"no sound from stream {stream!r} yet; streams: {', '.join(levels) or 'none'}")
    n = A.RATE * ms // 1000
    out = np.zeros((n, 2), np.float32)
    end = max((levels[name]["last_ms"] for name in names), default=0)
    for name in names:
        r = v.auto.audio_read(name, ms)
        x = np.frombuffer(r["pcm"], dtype=np.int16).reshape(-1, 2) / 32768.0
        if r["rate"] != A.RATE:
            idx = np.arange(round(len(x) * A.RATE / r["rate"])) * r["rate"] / A.RATE
            x = np.stack([np.interp(idx, np.arange(len(x)), x[:, c]) for c in (0, 1)], axis=1)
        x = x[-n:]
        # Streams arrive in blocks: line them up by when each last block came.
        shift = round((end - r["end_ms"]) * A.RATE / 1000)
        lo = max(0, n - len(x) - shift)
        hi = n - shift
        if hi > lo:
            out[lo:hi] += x[len(x) - (hi - lo):]
    return np.clip(out, -1, 1), names, end


@tool()
def sound_level(ms: int = 200) -> str:
    """How loud each sound stream was over the last ms of emulated time (up to 10 s): peak and RMS
    in dBFS (0 = full scale, -120 = digital silence). Streams: main (sound cards' digital audio),
    music (OPL FM and the PC speaker), wavetable, cd, fdd, hdd, ym2151, midi (synths). Sound comes
    in blocks of about 20 ms (CD 100 ms), so the newest few ms may not be in yet."""
    levels = vm().auto.audio_level(ms)
    if not levels:
        return "no sound streams yet"
    return "\n".join(f"{name:9} peak {l['peak_db']:6.1f} dB  rms {l['rms_db']:6.1f} dB  ({l['rate']} Hz)"
                     for name, l in levels.items())


@tool()
def wait_for_sound(threshold_db: float = -40.0, timeout_ms: int = 10000) -> str:
    """Run until any sound stream reaches threshold_db (peak, dBFS), or timeout_ms of emulated time
    passes. Reports which stream and the onset time (to about 1 ms; the run stops up to one 20 ms
    block later). For example, wait for a game's music or a beep before taking a screenshot."""
    a = vm().auto
    r = _wait(lambda t: a.wait_sound(t, threshold_db), timeout_ms)
    if r.get("matched"):
        return f"sound on {r['stream']} from t={r['onset_ms']:.1f}ms; {_time_note(r)}"
    return f"TIMED OUT: nothing reached {threshold_db} dB; {_time_note(r)}"


@tool()
def wait_for_silence(threshold_db: float = -60.0, for_ms: int = 500, timeout_ms: int = 10000) -> str:
    """Run until every sound stream has stayed below threshold_db for for_ms of emulated time, or
    timeout_ms passes. For example, wait for a sound effect or a track to finish."""
    a = vm().auto
    since = [a.status()["ms"]]

    def carry(reply):
        since[0] = reply.get("quiet_since_ms", since[0])

    r = _wait(lambda t: a.wait_silence(t, threshold_db, for_ms, since[0]), timeout_ms, carry)
    if r.get("matched"):
        return f"quiet since t={r['quiet_since_ms']}ms; {_time_note(r)}"
    return f"TIMED OUT: still sound above {threshold_db} dB; {_time_note(r)}"


@tool()
def sound_spectrogram(ms: int = 3000, stream: str | None = None) -> Image:
    """A spectrogram of the last ms of emulated sound (up to 10 s), all streams mixed or one: time
    left to right, frequency up to 8 kHz, brighter = louder. Shows at a glance whether there is
    music, speech, a steady tone (a horizontal line) or noise, and where it starts and stops. The
    dominant frequencies are in the title."""
    from . import audio as A

    x, names, end = _ring_mix(vm(), min(int(ms), 10000), stream)
    top = A.dominant_hz(x)
    title = (f"{', '.join(names) or 'silence'} until t={end}ms; " +
             ", ".join(f"{hz:.0f} Hz" for hz, _ in top))
    buf = io.BytesIO()
    A.spectrogram(x, title=title).save(buf, format="PNG")
    return Image(data=buf.getvalue(), format="png")


@tool()
def sound_save(path: str, ms: int = 5000, stream: str | None = None) -> str:
    """Save the last ms of emulated sound (up to 10 s), mixed or one stream, as a 48 kHz WAV, for a
    person to listen to (DC removed, as a real card's output capacitor does). Also reports the
    dominant frequencies."""
    from . import audio as A

    x, names, end = _ring_mix(vm(), min(int(ms), 10000), stream)
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    x = A.fade_edges(A.dc_block(x))
    A.write_wav(out, x)
    peak = A.to_db(float(abs(x).max())) if len(x) else -120
    top = ", ".join(f"{hz:.0f} Hz" for hz, _ in A.dominant_hz(x))
    return f"saved {len(x) / A.RATE:.2f} s of {', '.join(names) or 'silence'} to {out} (peak {peak:.1f} dB; {top})"


@tool()
def sound_check(directory: str | None = None) -> str:
    """Check a recording's sound (default: the last record_stop) for gaps: the recording itself
    (each stream's sample count against the emulated time it covered) and what the guest played:
    dropouts (silence inside sustained sound, as when a program fails to refill its sound buffer in
    time)."""
    from . import audio as A

    d = Path(directory).expanduser() if directory else _last_record_dir()
    segs = A.load(d)
    if not segs:
        return f"no sound recorded in {d}"
    lines = []
    for s in segs:
        line = f"{s.stream}: {s.frames / s.rate:.2f} s at {s.rate} Hz from t={s.start_ms:.1f}ms"
        if s.drift_frames is not None:
            line += f", {s.drift_frames:+.0f} frames ({s.drift_frames * 1000 / s.rate:+.2f} ms) against the clock" + (
                " (continuous)" if A.drift_ok(s) else " (FRAMES MISSING)")
        elif not s.exact:
            line += " (block-timed stream)"
        if s.lost:
            line += f"; {s.lost} frames LOST before they could be fetched from 86Box"
        gaps = A.find_gaps(A.read(s), s.start_ms)
        line += f"; {len(gaps)} dropouts"
        lines.append(line)
        for g in gaps[:8]:
            lines.append(f"  {g}")
        if len(gaps) > 8:
            lines.append(f"  ... {len(gaps) - 8} more")
    return "\n".join(lines)


_record_dirs: list[Path] = []


def _last_record_dir() -> Path:
    if not _record_dirs:
        raise ValueError("no recording made in this session")
    return _record_dirs[-1]


# Serial --------------------------------------------------------------------


@tool()
def serial_read(port: int = 1, from_start: bool = False) -> str:
    """Output the guest has written to a COM port since the last serial_read (from_start: since boot).

    Needs the port on the automation socket (vm_start(serial=[port])), or for COM1 on an older 86Box,
    vm_start(com1_log=True). The automation socket keeps the last 64 KiB. In DOS,
    `ECHO hello > COM1` or a program writing to the port shows up here; it is the easiest way to get
    debug logs out of your own programs.
    """
    text = vm().serial_read(port, from_start)
    return text if text else "(no new output)"


@tool()
def wait_for_serial(text: str, port: int = 1, timeout_ms: int = 10000, poll_ms: int = 20) -> str:
    """Run until `text` appears in new COM port output, or timeout_ms of emulated time passes.

    Returns the output seen while waiting, so a log line can be waited for and read in one call.
    """
    v = vm()
    if port in v.serial_ports:
        start = v.serial_ports[port]
        a = v.auto
        r = _wait(lambda t: a.wait_serial(port, start, text.encode("latin-1"), t), timeout_ms)
        seen = v.serial_read(port)
        if r.get("matched"):
            return f"found at t={r['ms']}ms\n{seen}"
        return f"TIMED OUT after {timeout_ms} ms\n{seen or '(no output)'}"
    seen = ""
    waited = 0
    while True:
        seen += v.serial_read(port)
        if text in seen:
            return f"found after {waited} ms\n{seen}"
        if waited >= timeout_ms:
            return f"TIMED OUT after {timeout_ms} ms\n{seen or '(no output)'}"
        _advance(poll_ms)
        waited += poll_ms


@tool()
def serial_write(text: str = "", port: int = 1, hex: str = "") -> str:
    """Send bytes to the guest on a COM port (started with vm_start(serial=[port])): text as it
    stands (Latin-1; "\\r" for Enter, "\\x1a" for Ctrl+Z), or hex. They reach the guest at the rate
    its UART is set to, as emulated time passes; up to 64 KiB can wait."""
    _serial_port(port)
    data = bytes.fromhex(hex) if hex else text.encode("latin-1")
    if not data:
        raise ValueError("nothing to send: give text or hex")
    queued = vm().auto.serial_in(port, data)
    if queued < len(data):
        raise AutomationError(f"only {queued} of {len(data)} bytes queued: the guest's input queue is full")
    return f"queued {queued} bytes for COM{port}"


# Files ---------------------------------------------------------------------


@tool()
def dos_put_floppy(paths: list[str], drive: str = "A") -> str:
    """Copy host files/directories onto a fresh 1.44 MB floppy image and insert it in drive A: or B:."""
    v = vm()
    img = v.make_floppy(paths)
    v.load_floppy(img, 0 if drive.upper().startswith("A") else 1)
    names = ", ".join(Path(p).name for p in paths)
    return f"inserted floppy with {names} in {drive.upper()[0]}:"


@tool()
def floppy_insert(image: str, drive: str = "A", write_protect: bool = False) -> str:
    """Insert an existing floppy image (.img/.ima/.86f, ...) in drive A: or B:. Works for any guest.

    With the Docker-run 86Box, images outside $HOME and /tmp are copied first. With
    write_protect=False the guest can write to the image file itself. For a VM joined with vm_attach,
    see there.
    """
    v = vm()
    v.load_floppy(image, 0 if drive.upper().startswith("A") else 1, write_protect)
    return f"inserted {Path(image).name} in {drive.upper()[0]}:"


@tool()
def floppy_eject(drive: str = "A") -> str:
    """Eject the floppy from drive A: or B:."""
    vm().auto.call(f"fdd eject {0 if drive.upper().startswith('A') else 1}")
    return f"ejected {drive.upper()[0]}:"


@tool()
def cdrom_insert(image: str | None = None, files: list[str] | None = None, drive: int = 0) -> str:
    """Insert a CD in the emulated CD-ROM drive: an existing image (.iso/.cue) or a new ISO built
    from host files and directories (ISO 9660 8.3 names plus Joliet long names).

    Works for any guest; DOS needs a CD-ROM driver and MSCDEX (or SHSUCDX) to read it. VMs made by
    vm_create/dos_create_vm have an ATAPI CD-ROM drive as primary slave (this board's BIOS leaves the
    secondary IDE channel off). The first read after a disc change may report "drive not ready"; retry.
    """
    v = vm()
    if (image is None) == (files is None):
        raise ValueError("pass exactly one of image or files")
    path = Path(image) if image else v.make_iso(files)
    v.load_cdrom(path, drive)
    mounted = [c["file"] for c in v.auto.info()["cdrom"] if c["drive"] == drive]
    if not mounted:
        raise AutomationError(f"86Box did not mount {path}; is there a CD-ROM drive {drive}? See vm_info.")
    return f"inserted {mounted[0]} in CD-ROM drive {drive}"


@tool()
def cdrom_eject(drive: int = 0) -> str:
    """Eject the CD from an emulated CD-ROM drive."""
    vm().auto.call(f"cd eject {drive}")
    return f"ejected CD-ROM drive {drive}"


# Memory and CPU ------------------------------------------------------------


CHUNK = 65536  # the automation socket's largest memr


def _check_range(addr: int, length: int) -> int:
    """Refuse addresses outside the emulated RAM; returns the RAM size in bytes."""
    size = vm().mem_bytes()
    if addr < 0 or length < 0 or addr + length > size:
        raise ValueError(f"{addr:#x}+{length} is outside the emulated RAM (0..{size:#x}, "
                         f"{size // 1024} KB; see vm_info)")
    return size


def _read_range(start: int, end: int) -> bytes:
    v = vm()
    return b"".join(v.auto.memr(a, min(CHUNK, end - a)) for a in range(start, end, CHUNK))


def _span(start: int, end: int | None) -> tuple[int, int]:
    size = vm().mem_bytes()
    end = size if end is None else end
    _check_range(start, end - start)
    return start, end


MEM_VIEWS = ("bus", "cpu", "normal", "smm", "linear")


def _bus_access(v: VM) -> bool:
    """Whether this 86Box's socket has memory views and access widths, and the ior/iow and pcir/pciw
    commands. Asked once per connection."""
    a = v.auto
    if getattr(a, "bus_access", None) is None:
        try:
            a.memr(0, 1, view="bus")
            a.bus_access = True
        except AutomationError:
            a.bus_access = False
    return a.bus_access


def _need_bus_access(v: VM, what: str) -> None:
    if not _bus_access(v):
        raise AutomationError(f"{what} needs an 86Box whose automation socket has memory views, access "
                              "widths and the ior/iow/pcir/pciw commands")


def _mem_args(addr: int, length: int, view: str, width: int) -> None:
    if view not in MEM_VIEWS:
        raise ValueError(f"view is one of {', '.join(MEM_VIEWS)}")
    if width not in (1, 2, 4):
        raise ValueError("width must be 1, 2 or 4")
    if not 0 <= length <= CHUNK or length % width:
        raise ValueError(f"length is 0..{CHUNK}, a multiple of the width")
    if not 0 <= addr or addr + length > 1 << 32:
        raise ValueError(f"{addr:#x}+{length} is outside the 4 GB address space")


@tool()
def read_memory(addr: int, length: int = 256, linear: bool = False, view: str = "bus", width: int = 1) -> str:
    """Hex dump of memory anywhere in the 4 GB address space: RAM, ROM, video memory, a device's
    memory-mapped registers (real-mode seg:off -> seg*16+off). Up to 65536 bytes.

    view picks whose view of memory: "bus" (the default) is what DMA and bus masters see, the
    physical memory map; "cpu" is the processor's as it is now (in SMM, that includes SMRAM);
    "normal" and "smm" are the processor's outside and inside SMM, whichever it is in; "linear"
    (or linear=True) reads at a linear address through the page tables, as the CPU sees it with
    paging on, with supervisor rights (translate_address shows the walk). width (1, 2 or 4) is the
    size of each access, for registers that must be read as words or dwords; the dump then shows
    little-endian values. Reading a device's registers can change its state (a FIFO, a status that
    clears on read), as the guest's own reads would."""
    if linear:
        view = "linear"
    _mem_args(addr, length, view, width)
    v = vm()
    if view == "bus" and width == 1:
        data = v.auto.memr(addr, length)
    elif view == "linear" and width == 1 and not _bus_access(v):
        try:        # an older 86Box: walk the page tables here, reading RAM only
            data = _linear_reader(v, _pm_regs(v))(addr, length)
        except ValueError as e:
            raise AutomationError(str(e))
    else:
        _need_bus_access(v, f'view="{view}", width={width}')
        data = v.auto.memr(addr, length, view=view, width=width)
    lines = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        if width == 1:
            vals = chunk.hex(" ")
        else:
            vals = " ".join(chunk[i:i + width][::-1].hex() for i in range(0, len(chunk), width))
        lines.append(f"{addr + off:08x}  {vals:<47}  {asc}")
    return "\n".join(lines)


@tool()
def write_memory(addr: int, hex_bytes: str | None = None, value: int | None = None, view: str = "bus",
                 width: int = 1) -> str:
    """Write bytes (hex string, e.g. "90 90 cc") anywhere in the 4 GB address space: RAM, video
    memory, a device's memory-mapped registers. Or value: one number, written as a single access
    of width bytes, little-endian. view and width are as for read_memory: width is the size of each
    access ("linear" writes through the page tables, read-only pages included). The device acts on
    the write at once, as if the guest had made it."""
    if (hex_bytes is None) == (value is None):
        raise ValueError("give hex_bytes or value")
    data = bytes.fromhex(hex_bytes) if hex_bytes is not None else value.to_bytes(width, "little", signed=value < 0)
    _mem_args(addr, len(data), view, width)
    v = vm()
    if view == "bus" and width == 1:
        v.auto.memw(addr, data)
    else:
        _need_bus_access(v, f'view="{view}", width={width}')
        v.auto.memw(addr, data, view=view, width=width)
    return f"wrote {len(data)} bytes at {addr:#x}" + (f" ({view})" if view != "bus" else "")


@tool()
def wait_for_memory(addr: int, hex_bytes: str, timeout_ms: int = 10000) -> str:
    """Run until physical memory at addr equals hex_bytes, or timeout_ms of emulated time passes."""
    data = bytes.fromhex(hex_bytes)
    _check_range(addr, len(data))
    a = vm().auto
    r = _wait(lambda t: a.wait_mem(addr, data, t), timeout_ms)
    return ("matched" if r.get("matched") else "TIMED OUT") + f"; {_time_note(r)}"


def _pattern(hex_bytes: str | None, text: str | None, value: int | None, size: int, signed: bool) -> bytes:
    given = [x is not None for x in (hex_bytes, text, value)]
    if sum(given) != 1:
        raise ValueError("give exactly one of hex_bytes, text or value")
    if hex_bytes is not None:
        return bytes.fromhex(hex_bytes)
    if text is not None:
        return text.encode("latin-1")
    if size not in (1, 2, 4):
        raise ValueError("size must be 1, 2 or 4")
    return value.to_bytes(size, "little", signed=signed)


@tool()
def find_memory(hex_bytes: str | None = None, text: str | None = None, value: int | None = None,
                size: int = 2, signed: bool = False, start: int = 0, end: int | None = None,
                max_results: int = 50) -> str:
    """Search physical memory for bytes (hex), text, or a little-endian number of `size` bytes.

    Searches start..end, by default the whole emulated RAM. For a real-mode DOS program, the first
    640 KB (end=0xA0000) is enough; DOS-extended programs and Windows use memory above 1 MB too.
    The BIOS's own count of conventional memory, in KB, is the word at 0x413.
    Lists the addresses where it occurs, at any alignment.
    """
    pat = _pattern(hex_bytes, text, value, size, signed)
    start, end = _span(start, end)
    data = _read_range(start, end)
    hits, i = [], data.find(pat)
    while i >= 0 and len(hits) < max_results:
        hits.append(start + i)
        i = data.find(pat, i + 1)
    more = " (more not shown)" if i >= 0 else ""
    return f"{len(hits)} found{more} in {start:#x}..{end:#x}: " + " ".join(f"{a:#x}" for a in hits)


_snapshots: dict[str, dict] = {}


@tool()
def snapshot_memory(name: str = "default", start: int = 0, end: int | None = None) -> str:
    """Remember the contents of start..end (default: all emulated RAM), for diff_memory.

    The way to find a variable: snapshot, make the game change it (move, score, lose a life), then
    diff_memory with the kind of change you expect. Each diff keeps only the addresses that matched,
    so repeat until a few are left.
    """
    start, end = _span(start, end)
    _snapshots[name] = {"start": start, "end": end, "data": _read_range(start, end), "candidates": None}
    return f"snapshot {name!r}: {end - start} bytes from {start:#x}"


def _num(buf: bytes, off: int, size: int, signed: bool) -> int:
    return int.from_bytes(buf[off:off + size], "little", signed=signed)


@tool()
def diff_memory(change: str = "changed", name: str = "default", size: int = 1, signed: bool = False,
                value: int | None = None, max_results: int = 50) -> str:
    """Compare memory with snapshot `name` and keep only the addresses that changed as described.

    change: "changed", "unchanged", "increased", "decreased", or "equals" (now equal to value).
    size: 1, 2 or 4 bytes, read little-endian, signed or not. The first diff after a snapshot looks
    at every address; later ones only at the survivors, and the snapshot then moves to the present.
    """
    if change not in ("changed", "unchanged", "increased", "decreased", "equals"):
        raise ValueError("change must be changed, unchanged, increased, decreased or equals")
    if change == "equals" and value is None:
        raise ValueError("change='equals' needs value")
    if size not in (1, 2, 4):
        raise ValueError("size must be 1, 2 or 4")
    snap = _snapshots.get(name)
    if snap is None:
        raise ValueError(f"no snapshot {name!r}; call snapshot_memory first")
    start, end, old = snap["start"], snap["end"], snap["data"]
    new = _read_range(start, end)

    def keep(off: int) -> bool:
        if off + size > len(new):
            return False
        a, b = _num(old, off, size, signed), _num(new, off, size, signed)
        return {"changed": a != b, "unchanged": a == b, "increased": b > a, "decreased": b < a,
                "equals": b == value}[change]

    if snap["candidates"] is not None:
        survivors = [off for off in snap["candidates"] if keep(off)]
    elif change in ("unchanged", "equals"):
        survivors = [off for off in range(len(new) - size + 1) if keep(off)]
    else:
        # Only bytes that differ can start a changed value: look near them, chunk by chunk.
        survivors = []
        for c in range(0, len(new), 4096):
            if old[c:c + 4096] == new[c:c + 4096]:
                continue
            for off in range(max(0, c - size + 1), min(len(new), c + 4096)):
                if (not survivors or survivors[-1] < off) and keep(off):
                    survivors.append(off)
    snap["candidates"], snap["data"] = survivors, new
    shown = survivors[:max_results]
    def fmt(off):
        return f"{start + off:#x}={_num(new, off, size, signed)}"
    more = f" (showing {len(shown)})" if len(survivors) > len(shown) else ""
    return f"{len(survivors)} addresses {change}{more}: " + " ".join(fmt(o) for o in shown)


@tool()
def registers() -> dict:
    """CPU registers at the current instruction boundary (sampled between 1 ms quanta).

    General registers, eip, eflags, segment selectors and cr0 on every CPU. From the 286, each
    segment's descriptor cache (ds_base, ds_limit, ds_ar...; ar = descriptor bytes 5-6), gdtr/idtr
    base and limit, ldtr and tr, and cpl. From the 386, cr2 (last page-fault address), cr3 (page
    directory base), dr0-dr3, dr6, dr7 and smm. cr4 and tsc where the CPU has them. x86 has no CR1.
    The x87 and MMX registers are in fpu_registers(). Needs 86Box with 86Box/86Box#8416 for more
    than eip, eflags, selectors, cs_base and cr0."""
    return vm().auto.regs()


@tool()
def fpu_registers() -> dict:
    """The x87 FPU: fcw, fsw (with TOP), ftw (full tag word, 2 bits per physical register), top,
    st0-st7 as 20 hex digits (sign+exponent, then significand, as FSAVE stores them), and with
    MMX mm0-mm7 as 16 hex digits. Fails on a machine without an FPU. Needs 86Box/86Box#8416."""
    return vm().auto.fpu()


# Protected mode: descriptor tables and page tables, read through the registers above ------------


def _pm_regs(v: VM) -> dict:
    r = v.auto.regs()
    if "gdtr_base" not in r:
        raise AutomationError("this 86Box doesn't report the descriptor-table and control registers; "
                              "it needs 86Box/86Box#8416")
    return r


def _phys_reader(v: VM):
    size = v.mem_bytes()

    def read(addr: int, length: int) -> bytes:
        if addr < 0 or addr + length > size:
            raise ValueError(f"physical {addr:#x}+{length} is outside the emulated RAM (0..{size:#x})")
        return v.auto.memr(addr, length)
    return read


def _linear_reader(v: VM, r: dict):
    phys = _phys_reader(v)
    return lambda addr, length: x86.read_linear(phys, r["cr0"], r.get("cr3", 0), r.get("cr4", 0), addr, length)


@tool()
def descriptor_table(table: str = "gdt", first: int = 0, count: int = 16) -> str:
    """Decode entries of the GDT, LDT or IDT (table "gdt", "ldt" or "idt"), from the CPU's GDTR,
    LDTR or IDTR: base, limit, type, DPL and flags for segments; selector:offset for gates.
    first is the entry index (a selector >> 3, or an interrupt vector). In real mode the IDT is
    the interrupt vector table (seg:off per vector). The tables are read at their linear
    addresses, through the page tables when paging is on."""
    v = vm()
    r = _pm_regs(v)
    table = table.lower()
    if table not in ("gdt", "ldt", "idt"):
        raise ValueError('table is "gdt", "ldt" or "idt"')
    base, limit = r[f"{table}r_base"], r[f"{table}r_limit"]
    real = not r["cr0"] & x86.CR0_PE
    size = 4 if (table == "idt" and real) else 8
    total = (limit + 1) // size
    head = f"{table.upper()} base {base:#010x} limit {limit:#06x} ({total} entries), CPU {x86.mode(r)}"
    if table == "ldt":
        if real or not r.get("ldtr"):
            return head + "\nno LDT loaded (LDTR is 0)"
        head += f", LDTR {r['ldtr']:04x}"
    if table == "gdt" and real:
        head += " (real mode: the GDT isn't in use)"
    read = _linear_reader(v, r)
    lines = [head]
    for i in range(max(first, 0), min(first + count, total)):
        try:
            raw = read(base + i * size, size)
        except ValueError as e:
            lines.append(f"{i:4x}  {e}")
            break
        if size == 4:
            off, seg = int.from_bytes(raw[:2], "little"), int.from_bytes(raw[2:], "little")
            lines.append(f"{i:4x}  {seg:04x}:{off:04x}")
            continue
        sel = f"sel {i * 8 | (4 if table == 'ldt' else 0):04x}  " if table != "idt" else "     "
        lines.append(f"{i:4x}  {sel}{x86.decode_descriptor(raw)}")
    if first >= total:
        lines.append(f"(entry {first:#x} is past the limit)")
    return "\n".join(lines)


def _segment_base(v: VM, r: dict, selector: int | str) -> tuple[int, int | None, str]:
    """Base, limit (None in real or V86 mode) and a description of a selector or segment register."""
    if isinstance(selector, str):
        name = selector.lower()
        if name not in ("cs", "ds", "es", "ss", "fs", "gs"):
            raise ValueError("a segment register is cs, ds, es, ss, fs or gs")
        base = r["cs_base"] if name == "cs" else r[f"{name}_base"]
        limit = r.get(f"{name}_limit")
        return base, limit, f"{name} = {r[name]:04x}, cached base {base:#x} limit {limit:#x}"
    mode = x86.mode(r)
    if mode in ("real", "virtual-8086"):
        return selector * 16, None, f"{mode} mode: {selector:04x} * 16"
    table = "ldt" if selector & 4 else "gdt"
    tbase, tlimit = r[f"{table}r_base"], r[f"{table}r_limit"]
    if (selector & ~7) + 7 > tlimit:
        raise ValueError(f"selector {selector:04x} is past the {table.upper()} limit {tlimit:#x}")
    d = x86.decode_descriptor(_linear_reader(v, r)(tbase + (selector & ~7), 8))
    if d.selector is not None:      # a TSS or LDT descriptor still has a base worth translating
        raise ValueError(f"selector {selector:04x} is a {d.kind}, not a segment")
    note = "" if d.present else "\nthe segment isn't present: loading it would fault (#NP, or #SS for SS)"
    return d.base, d.limit, f"selector {selector:04x} ({table.upper()} entry {selector >> 3:#x}, RPL {selector & 3}): {d}{note}"


@tool()
def translate_address(address: int, selector: int | str | None = None) -> str:
    """Translate an address as the CPU would: selector:offset -> linear -> physical. With selector
    (a number, looked up in the GDT or LDT, or a segment register name such as "ds", using its
    cached base), address is the offset; without, address is already linear. The page walk shows
    each page directory and table entry. In real and V86 mode a selector is a paragraph number."""
    v = vm()
    r = _pm_regs(v)
    lines = [f"CPU {x86.mode(r)}"]
    linear = address
    if selector is not None:
        base, limit, desc = _segment_base(v, r, selector)
        lines.append(desc)
        if limit is not None and address > limit:
            lines.append(f"offset {address:#x} is past the limit {limit:#x}: the CPU would fault")
        linear = (base + address) & 0xFFFFFFFF
    lines.append(str(x86.walk(_phys_reader(v), r["cr0"], r.get("cr3", 0), r.get("cr4", 0), linear)))
    return "\n".join(lines)


# Debugging, through 86Box's GDB stub ------------------------------------------------------------


def _debugger() -> tuple[VM, Gdb]:
    v = vm()
    if v.gdb is None:
        raise AutomationError("no debugger: start with vm_start(debugger=True), which needs 86Box built "
                              "with -D GDBSTUB=ON")
    return v, v.gdb


def _linear_eip(g: Gdb) -> int:
    return g.regs()["eip"]      # the stub reports eip as a linear address (CS base + EIP)


def _stop_cpu(v: VM, g: Gdb) -> None:
    """Make sure the stub has the CPU stopped at an instruction boundary."""
    if v.gdb_stopped:
        return
    g.sock.sendall(b"\x03")     # interrupt; the stub stops the CPU when it next runs
    with v.expecting_stop():
        v.auto.step(1)
    g.recv(timeout=10)
    v.gdb_stopped = True


def _single_step(v: VM, g: Gdb) -> str:
    """One instruction, stepping over an execution breakpoint at the current address."""
    here = _linear_eip(g)
    over = [(a, k, n) for (a, k, n) in v.breaks if k == "exec" and a == here]
    for a, k, n in over:
        g.clear_break(a, k, n)
    g.send("s")
    with v.expecting_stop():
        v.auto.step(1)
    stop = g.recv(timeout=10)
    for a, k, n in over:
        g.set_break(a, k, n)
    v.gdb_stopped = True
    return stop


def _where(v: VM, g: Gdb) -> str:
    r = v.auto.regs()
    lin = _linear_eip(g)
    try:
        code = _linear_reader(v, r)(lin, 8).hex(" ") if "cr3" in r else v.auto.memr(lin, 8).hex(" ")
    except (ValueError, AutomationError):
        code = "?"
    return f"{r['cs']:04x}:{r['eip']:08x} (linear {lin:#x}), bytes {code}"


@tool()
def breakpoint_set(addr: int, kind: str = "exec", length: int = 1, clear: bool = False) -> str:
    """Stop the CPU when it executes the instruction at addr, or when it writes, reads or accesses
    memory at addr..addr+length (kind "exec", "write", "read" or "access"). clear=True removes it.

    Addresses are linear: in real mode, segment*16 + offset; without paging, physical. Then
    run_until_break runs to it. Watchpoints stop after the access, with the CPU at the next
    instruction. Needs vm_start(debugger=True).
    """
    v, g = _debugger()
    if kind not in BREAK_KINDS or kind == "exec_sw":
        raise ValueError("kind must be exec, write, read or access")
    key = (addr, kind, length)
    if clear:
        g.clear_break(addr, kind, length)
        v.breaks.discard(key)
        return f"cleared {kind} breakpoint at {addr:#x}"
    g.set_break(addr, kind, length)
    v.breaks.add(key)
    return f"{kind} breakpoint at {addr:#x}" + (f" ({length} bytes)" if kind != "exec" else "") + \
        f"; {len(v.breaks)} set"


def _resume(v: VM, g: Gdb) -> None:
    if v.gdb_stopped:
        _single_step(v, g)      # over a breakpoint at the current instruction
        g.cont()
        v.gdb_stopped = False


@tool()
def debug_continue() -> str:
    """Let the CPU go on after a breakpoint or step_instructions, so step, press_keys, dos_run and
    the rest advance the machine again. Breakpoints stay set; a hit during another tool's call stops
    the CPU and that call reports it."""
    v, g = _debugger()
    was = v.gdb_stopped
    _resume(v, g)
    return "the CPU runs again (held: time passes in tool calls)" if was else "the CPU wasn't stopped"


@tool()
def run_until_break(timeout_ms: int = 10000) -> str:
    """Run until a breakpoint or watchpoint stops the CPU, or timeout_ms of emulated time passes.

    Reports where the CPU stopped and why. Running on from a breakpoint steps over it first. The
    CPU then stays stopped, and tools that need time to pass (step, press_keys, dos_run...) fail
    until debug_continue.
    """
    v, g = _debugger()
    _resume(v, g)
    with v.expecting_stop():
        r = v.auto.step(timeout_ms)
    if r.get("stopped") != "debugger":
        return f"no break in {timeout_ms} ms; {_time_note(r)}"
    why = describe_stop(g.recv(timeout=10))
    return f"{why} at {_where(v, g)}; emulated t={r['ms']}ms"


@tool()
def step_instructions(count: int = 1) -> str:
    """Execute count instructions (up to 200) one at a time, listing each address and its first bytes.

    Stops the CPU first if it's running. Afterwards the CPU stays stopped: debug_continue or
    run_until_break go on.
    Each instruction counts as one 1 ms quantum on the automation clock (the guest itself sees only
    the instruction's own cycles).
    """
    if not 1 <= count <= 200:
        raise ValueError("count must be 1..200")
    v, g = _debugger()
    _stop_cpu(v, g)
    lines = [f"start  {_where(v, g)}"]
    for i in range(count):
        _single_step(v, g)
        lines.append(f"{i + 1:5d}  {_where(v, g)}")
    return "\n".join(lines)


_WIDTHS = {1: "b", 2: "w", 4: "l"}


def _io_values(out: str) -> list[int]:
    return [int(x, 16) for line in out.splitlines() if ":" in line for x in line.split(":", 1)[1].split()]


def _io_in(v: VM, port: int, width: int, count: int) -> list[int]:
    """IN from count ports upward: through the automation socket, or on an older 86Box, the GDB stub."""
    if _bus_access(v):
        return v.auto.ior(port, width, count)
    _, g = _debugger()
    return _io_values(g.monitor(f"i{_WIDTHS[width]} {port:x} {count * width:x}"))


def _io_out(v: VM, port: int, width: int, value: int) -> None:
    if _bus_access(v):
        v.auto.iow(port, width, [value])
    else:
        _, g = _debugger()
        g.monitor(f"o{_WIDTHS[width]} {port:x} {value:x}")


@tool()
def io_read(port: int, width: int = 1, count: int = 1) -> str:
    """Read count values of width bytes (1, 2 or 4) from I/O port `port` upward, as the CPU's IN would.

    Reading a port can change the device's state (a status register that clears on read, a FIFO).
    Works while held. An 86Box whose automation socket predates I/O commands needs
    vm_start(debugger=True).
    """
    if width not in _WIDTHS or not 1 <= count <= 256 or not 0 <= port <= 0xFFFF:
        raise ValueError("port must be 0..0xffff, width 1, 2 or 4, and count 1..256")
    vals = _io_in(vm(), port, width, count)
    return f"port {port:#x}: " + " ".join(f"{x:#0{2 + 2 * width}x}" for x in vals)


@tool()
def io_write(port: int, value: int, width: int = 1) -> str:
    """Write value (width 1, 2 or 4 bytes) to I/O port `port`, as the CPU's OUT would.

    The device acts on it at once, as if the guest had written it. Works while held. An 86Box
    whose automation socket predates I/O commands needs vm_start(debugger=True).
    """
    if width not in _WIDTHS or not 0 <= port <= 0xFFFF:
        raise ValueError("port must be 0..0xffff, and width 1, 2 or 4")
    _io_out(vm(), port, width, value & ((1 << (8 * width)) - 1))
    return f"wrote {value:#x} to port {port:#x}"


def _pci_address(bus: int, device: int, function: int) -> None:
    if not (0 <= bus <= 255 and 0 <= device <= 31 and 0 <= function <= 7):
        raise ValueError("bus is 0..255, device 0..31, function 0..7")


_PCI_CLASSES = {0x00: "unclassified", 0x01: "storage", 0x02: "network", 0x03: "display", 0x04: "multimedia",
                0x05: "memory", 0x06: "bridge", 0x07: "communication", 0x08: "system", 0x09: "input",
                0x0C: "serial bus"}


def _pci_dwords(v: VM, bus: int, dev: int, fn: int, offsets: list[int]) -> list[int]:
    """Read config dwords: straight from the function through the automation socket, or on an older
    86Box, through configuration mechanism #1 (ports 0xCF8/0xCFC) with the GDB stub, restoring 0xCF8."""
    if _bus_access(v):
        return [int.from_bytes(v.auto.pcir(bus, dev, fn, off & 0xFC, 4, 4)[1], "little") for off in offsets]
    _, g = _debugger()
    saved = _io_values(g.monitor("il cf8 4"))[0]
    out = []
    for off in offsets:
        g.monitor(f"ol cf8 {0x80000000 | bus << 16 | dev << 11 | fn << 8 | (off & 0xFC):x}")
        out.append(_io_values(g.monitor("il cfc 4"))[0])
    g.monitor(f"ol cf8 {saved:x}")
    return out


@tool()
def pci_scan(bus: int = 0) -> str:
    """List the PCI functions on `bus`: address, vendor:device ID, class, and interrupt line/pin.

    PCI machines only. Reads each function's configuration space directly, without touching the
    guest's 0xCF8; an 86Box whose automation socket predates that goes through configuration
    mechanism #1 and needs vm_start(debugger=True).
    """
    _pci_address(bus, 0, 0)
    v = vm()
    lines = []
    for dev in range(32):
        for fn in range(8):
            ident, cls, hdr, irq = _pci_dwords(v, bus, dev, fn, [0x00, 0x08, 0x0C, 0x3C])
            if ident in (0xFFFFFFFF, 0):
                if fn == 0:
                    break
                continue
            klass = _PCI_CLASSES.get(cls >> 24, f"class {cls >> 24:#04x}")
            pin = "ABCD"[((irq >> 8) & 0xFF) - 1] if 1 <= (irq >> 8) & 0xFF <= 4 else "-"
            lines.append(f"{bus:02x}:{dev:02x}.{fn} {ident & 0xFFFF:04x}:{ident >> 16:04x} {klass} "
                         f"({cls >> 8:06x}) irq line {irq & 0xFF} pin {pin}")
            if fn == 0 and not (hdr >> 16) & 0x80:
                break       # not a multi-function device
    return "\n".join(lines) or f"no PCI functions on bus {bus}"


@tool()
def pci_config_read(bus: int, device: int, function: int = 0, offset: int = 0, length: int = 64) -> str:
    """Hex dump of a PCI function's configuration space, offset..offset+length (up to 256), as
    dwords. Like pci_scan, needs vm_start(debugger=True) only on an older 86Box."""
    _pci_address(bus, device, function)
    if not (0 <= offset < 256 and 1 <= length <= 256 - offset):
        raise ValueError("offset and length must stay within the 256-byte configuration space")
    start = offset & ~3
    offs = list(range(start, offset + length, 4))
    vals = _pci_dwords(vm(), bus, device, function, offs)
    rows = [f"{o:02x}: {v:08x}" for o, v in zip(offs, vals)]
    return f"{bus:02x}:{device:02x}.{function} config:\n" + "\n".join(rows)


@tool()
def pci_config_write(bus: int, device: int, function: int, offset: int, value: int, width: int = 4) -> str:
    """Write value to a PCI function's configuration space at offset, as one configuration cycle
    of width bytes (1, 2 or 4), straight to the function (the guest's 0xCF8 is left alone). The
    function acts on it at once: a BAR moves, a command bit turns decoding on or off."""
    _pci_address(bus, device, function)
    if width not in _WIDTHS or not 0 <= offset <= 256 - width:
        raise ValueError("width must be 1, 2 or 4, and offset..offset+width within the 256 bytes")
    v = vm()
    _need_bus_access(v, "pci_config_write")
    data = (value & ((1 << (8 * width)) - 1)).to_bytes(width, "little")
    if not v.auto.pciw(bus, device, function, offset, data, width):
        raise AutomationError(f"no PCI device at {bus:02x}:{device:02x}")
    return f"wrote {value:#x} to {bus:02x}:{device:02x}.{function} offset {offset:#04x}"


_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


def _http_security(host: str, tls: bool):
    """DNS-rebinding protection for a loopback address, allowing https:// origins too (the MCP
    library's own default allows only http://). None, so no checks, on any other address: remote
    clients send that address as their Host, which only insecure mode allows."""
    from mcp.server.transport_security import TransportSecuritySettings

    if host not in _LOOPBACK:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    loopback = ("127.0.0.1", "localhost", "[::1]")
    schemes = ("http", "https") if tls else ("http",)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in loopback],
        allowed_origins=[f"{scheme}://{h}:*" for scheme in schemes for h in loopback],
    )


def _run_http(host: str, port: int, cert: str | None, key: str | None) -> None:
    """Streamable HTTP, over TLS with a certificate. The MCP library's own runner has no TLS
    options, so run its app with uvicorn here."""
    import uvicorn

    app = mcp.streamable_http_app(transport_security=_http_security(host, bool(cert)), host=host)
    tls = {"ssl_certfile": cert, "ssl_keyfile": key} if cert else {}
    uvicorn.run(app, host=host, port=port, log_level=mcp.settings.log_level.lower(), **tls)


def main() -> None:
    from .vm import INSECURE

    httpport = os.environ.get('BOX86_MCP_HTTP_PORT', '0')
    host = os.environ.get("BOX86_MCP_HTTP_HOST", "127.0.0.1")
    cert, key = os.environ.get("BOX86_MCP_TLS_CERT"), os.environ.get("BOX86_MCP_TLS_KEY")
    if bool(cert) != bool(key):
        raise SystemExit("set both BOX86_MCP_TLS_CERT and BOX86_MCP_TLS_KEY (PEM files), or neither")
    if httpport == '0':
        if cert:
            raise SystemExit("BOX86_MCP_TLS_CERT and BOX86_MCP_TLS_KEY need BOX86_MCP_HTTP_PORT")
        mcp.run()
        return
    if host not in _LOOPBACK:
        if not INSECURE:
            raise SystemExit(f"BOX86_MCP_HTTP_HOST={host} is not a loopback address: anyone who can reach "
                             "it could drive the VM and read and write files as you. Set BOX86_INSECURE=1 "
                             "if that's what you want.")
        print(f"box86mcp: WARNING: insecure mode: serving MCP without authentication on {host}:{httpport}; "
              "anyone who can reach it can drive the VM and read and write files as this user",
              file=sys.stderr, flush=True)
    _run_http(host, int(httpport), cert, key)

if __name__ == "__main__":
    main()
