# Changes to 86Box

Everything this project needs from 86Box is now in upstream 86Box `master`.
Both pull requests were merged on 2026-10-17:

| Pull request | What it adds |
|---|---|
| [#8365](https://github.com/86Box/86Box/pull/8365) | The automation socket (`mouse: let input through when an automation client sends it` and `Add an opt-in automation control socket`) |
| [#8361](https://github.com/86Box/86Box/pull/8361) | `char_file: add an option to write output to the file immediately`. 86box-mcp's COM1 capture turns the option on. |

Two later pull requests add commands the server uses:

| Pull request | What it adds |
|---|---|
| [#8382](https://github.com/86Box/86Box/pull/8382) | `quit soft`: press the power button (`vm_stop(soft=True)`) |
| [#8384](https://github.com/86Box/86Box/pull/8384) | `ff on\|off`, and `ff` in `status` (the `fast_forward` tool) |

Later fixes and additions, all merged by 2026-10-18:

| Pull request | What it adds |
|---|---|
| [#8390](https://github.com/86Box/86Box/pull/8390) | The automation socket and the GDB stub share a machine: GDB answers while held, and the clock doesn't count time the stub has the CPU stopped (the debugger tools) |
| [#8391](https://github.com/86Box/86Box/pull/8391) | GDB stub watchpoints see accesses through the memory lookup cache, so they fire on ordinary RAM |
| [#8392](https://github.com/86Box/86Box/pull/8392) | The automation socket closes when 86Box hits a fatal error, instead of going silent |
| [#8396](https://github.com/86Box/86Box/pull/8396) | A client's memory reads aren't charged to the emulated CPU |
| [#8397](https://github.com/86Box/86Box/pull/8397) | `--automation-insecure`: TCP without the token (`BOX86_INSECURE=1`) |
| [#8402](https://github.com/86Box/86Box/pull/8402) | Warning dialogs don't stop an automated 86Box, and a CD image that won't load is reported to the client |

Merged on 2026-10-19:

| Pull request | What it adds |
|---|---|
| [#8405](https://github.com/86Box/86Box/pull/8405) | `tablet x y buttons` / `tablet out`: absolute input for a configured tablet or touchscreen (the `tablet` tool) |
| [#8416](https://github.com/86Box/86Box/pull/8416) | `regs` returns CR2, CR3, CR4, the debug registers, the descriptor caches, GDTR/IDTR/LDTR/TR, CPL, SMM and the TSC; a new `fpu` command returns the x87 and MMX registers (`registers`, `fpu_registers`, and the protected-mode tools built on them). There is no CR1 on x86 |

Merged on 2026-10-20, for sound and for recording a VM on another machine:

| Pull request | What it adds |
|---|---|
| [#8419](https://github.com/86Box/86Box/pull/8419) | `pc_run` blocks average exactly 1 ms of emulated time. Without it, for CPU clocks that aren't a multiple of 1 kHz, the socket's clock runs 10 ppm fast against the timers |
| [#8420](https://github.com/86Box/86Box/pull/8420) | Sound capture on the emulated clock: `audio start`/`stop`, `audio level`, `audio read`, `audio pull` (over the connection, for a client on another machine), `wait sound`, `wait silence`, and per-frame cycle and sample stamps (`frames.jsonl`) for recordings |
| [#8421](https://github.com/86Box/86Box/pull/8421) | Recorded frames over the connection (`record start` without a directory, `record pull`; zstd, XORed with the previous frame), so a client on another machine gets a recording's frames. Stacked on #8420 |

Also merged on 2026-10-20, for serial ports and repeatable runs:

| Pull request | What it adds |
|---|---|
| [#8439](https://github.com/86Box/86Box/pull/8439) | The `automation` serial backend (`serialN_device = automation`): `serial read`, `serial write`, `serial lines`, `wait serial`, and COM ports in `info` (`vm_start(serial=...)`, `serial_write`, `serial_link`, `serial_attach`). Input reaches the guest at its line rate |
| [#8438](https://github.com/86Box/86Box/pull/8438) | `--automation-seed n`: 86Box's random numbers (generated MACs, 86F weak bits) from a fixed seed instead of the host's timestamp counter |

Merged on 2026-10-22, for driving an 86Box on another machine
([#8466](https://github.com/86Box/86Box/pull/8466); its doc follow-up,
[#8467](https://github.com/86Box/86Box/pull/8467), warning never to put a
remote-enabled 86Box on the internet, merged the same day):

| Change | What it adds |
|---|---|
| `media put`, `media remove` | A client sends a file (raw bytes after the request line), which lands in the VM directory's `automation-media` folder, to load with `fdd load` / `cd load` (`floppy_insert`, `cdrom_insert`, `dos_put_floppy` on an attached VM) |
| `log` | The last 64 KB of 86Box's log (`vm_status`'s log tail on an attached VM) |
| fatal errors | A connected client gets `{"ok":false,"fatal":true,"error":...}` before the socket closes |
| `info` `gdbstub_port` | The GDB stub's port, so an attached VM's debugger finds it |

Open, for memory anywhere and devices without the debugger
([#8584](https://github.com/86Box/86Box/pull/8584)):

| Change | What it adds |
|---|---|
| `memr`/`memw` views and widths | Memory through the bus's view, the CPU's (now, outside SMM, or in SMM, for SMRAM), or a linear address through the page tables, in 1-, 2- or 4-byte accesses (`read_memory`/`write_memory` with `view` and `width`) |
| `ior`, `iow` | Port I/O at any width (`io_read`, `io_write` without the debugger) |
| `pcir`, `pciw` | PCI configuration space straight to the function, leaving 0xCF8 alone (`pci_scan`, `pci_config_read` without the debugger, and `pci_config_write`) |

Until it is merged, the server asks once per connection whether the socket has
these. Without them, `read_memory` and `write_memory` still work in the bus's
view a byte at a time, `linear=True` walks the page tables in Python as
before, and the I/O and PCI tools use the GDB stub.

`86BOX_MIN_COMMIT` is the minimum 86Box for every MCP tool: an upstream
commit with all of the above, the merge of #8466 (`1be46d317`, 2026-10-22).
Any later master works too; the build scripts only warn about an older one.
The server checks for `ff` in `status` before sending `quit soft`, because an
older 86Box takes any `quit ...` as a hard quit.

Build 86Box with `-D AUTOMATION=ON`; it's off by default, so official releases
don't have the socket. [automation-code-guide.md](automation-code-guide.md)
walks through the code.

When this project needs something new from 86Box, record it here: what
changes, why, and the pull request. Make it on a fresh branch from upstream
`master`.
