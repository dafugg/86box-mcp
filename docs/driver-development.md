# Developing drivers and device emulation

Who this is for: someone working on a device emulation in 86Box, or on a
driver for an emulated device, with a guest OS such as Windows 98 that already
has drivers for that hardware. The guest's driver is then the test suite: does
it detect the device, program it the way the datasheet says, and survive
interrupts, resets and odd timing?

## What works today

Without anything extra:

| Need | Tools |
|---|---|
| Repeatable runs, and time that stops while you look | `vm_start` (held), `step(ms)`, `hold`, `release` |
| What the guest sees on screen, frame by frame | `screenshot`, `record_start`, `record_stop` |
| Memory: driver state, DMA buffers, descriptor rings | `read_memory`, `write_memory`, `find_memory`, `snapshot_memory`, `diff_memory`, `wait_for_memory` |
| The device's memory-mapped registers and frame buffer, SMRAM, a driver's virtual addresses | `read_memory`/`write_memory` with `width=2` or `4`, and `view="cpu"`, `"smm"` or `"linear"` |
| Read and write the device's registers as the driver would | `io_read(port, width, count)`, `io_write(port, value, width)` |
| See how the BIOS and the OS set up PCI, and change it | `pci_scan`, `pci_config_read`, `pci_config_write` |
| CPU state at a 1 ms quantum boundary | `registers` |
| Resets and crashes, including triple faults | `reset`, `vm_info` (`hard_resets`, `soft_resets`, `last_reset_ms`) |
| Debug output from the guest | `serial_read`, `wait_for_serial` (COM1) |
| Media changes while running | `floppy_insert`, `cdrom_insert` (an ISO built from host files) |
| Driver installation and file transfer inside Windows | [V9x Remote Agent](../examples/v9x/) next to 86box-mcp |

With `vm_start(debugger=True)`, which uses 86Box's GDB stub:

| Need | Tools |
|---|---|
| Stop where the driver runs: its interrupt handler, an entry point | `breakpoint_set(addr)`, then `run_until_break` |
| Catch who writes a buffer or a variable | `breakpoint_set(addr, "write", length)`, also `"read"` and `"access"` |
| Follow the driver one instruction at a time | `step_instructions(count)` |
| Go on after a stop | `debug_continue`, or `run_until_break` again |

On an 86Box whose automation socket predates the `ior`/`iow`/`pcir` commands, `io_read`, `io_write`,
`pci_scan` and `pci_config_read` need the debugger too.

All of these work while the machine is held. Watchpoints stop the CPU at the
instruction after the access. Addresses are linear: in real mode that's
segment × 16 + offset, without paging it's physical, and under Windows it's
the virtual address. While the debugger has the CPU stopped, tools that need
time to pass (`step`, `press_keys`, `dos_run` and the rest) fail with an
error until `debug_continue`. A breakpoint that hits during one of those calls
stops the CPU, and the call reports where.

Reading a port is a real read. A status register that clears when read, or a
FIFO, changes just as if the driver had read it.

### Setting up the debugger

1. Build 86Box with the stub as well as the automation socket:
   `docker/build.sh -D GDBSTUB=ON`, or add `-D GDBSTUB=ON` to your own
   CMake line.
2. Use an 86Box from 2026-10-19 or later (`86BOX_MIN_COMMIT` or newer), with two
   fixes. [86Box#8390](https://github.com/86Box/86Box/pull/8390) lets the stub and the
   automation socket share a machine: without it, GDB gets no answers while the
   machine is held, and the automation clock counts time while the stub has the
   CPU stopped. [86Box#8391](https://github.com/86Box/86Box/pull/8391) makes
   memory watchpoints fire on ordinary RAM.
3. `vm_start(vm_dir, debugger=True)`.

A build with the stub also works without `debugger=True`. The stub starts the
CPU stopped and waits for a debugger, so the server attaches once and lets
it go.

**Security:** 86Box's stub listens on every network interface, not just
localhost, with no authentication, and a client has full control of the
machine. `debugger=True` gives each VM a free port (the default is 12345 for
all of them). Firewall the host, or keep stub builds off shared networks.

**Clock:** each instruction run by `step_instructions` counts as one 1 ms
quantum on the automation clock (`ms` in replies). The guest's own timers only
see the instruction's cycles.

## Example: a guest's timer interrupt, under DOS

From `server/tests/test_gdb.py`, on a 486 running DOS 6.22:

```
breakpoint_set(0xdbec)            # DOS's INT 08h handler, from the IVT at 0x20
run_until_break(2000)             # breakpoint at 0dbb:0000003c (linear 0xdbec), bytes e8 08 01 ...
step_instructions(4)              # the CALL, then PUSH AX, PUSH BP, PUSH ES...
run_until_break(2000)             # the next tick, 55 ms later
breakpoint_set(0x46c, "write", 4) # the BIOS tick count
run_until_break(2000)             # write to 0x46c at f000:0000d2fe (the BIOS's handler)
io_write(0x70, 0x0a); io_read(0x71)   # CMOS status register A: 0x26
pci_scan()                        # 00:05.0 1039:0496 bridge (the SiS 496)
```

The same works for a Windows 98 driver: find its interrupt handler, or the
buffer it hands the device, then break or watch there.

## What else a driver developer would want

None of these exist yet. Each says what 86Box has now and what adding it would
take.

| Feature | Why | What it would take |
|---|---|---|
| **I/O port trace** | A log of every `IN` and `OUT` in a port range, with emulated time, CS:EIP, width and value, shows exactly how a driver programs the device, and compares with the datasheet or with a real card's trace | The highest-value addition. A hook in `inb`/`outb` and friends (`src/io.c`), off unless a trace is active, writing to a ring buffer that an automation command reads |
| **I/O port breakpoints** | Stop when the driver touches a register, without knowing where its code is | The same hook, stopping the CPU through the stub. The guest's own debug registers can't help: they belong to the guest |
| **MMIO trace** | The same for memory-mapped devices (PCI BARs, framebuffers) | A hook in the memory mapping layer. Watchpoints may already catch MMIO accesses, which don't go through the RAM lookup cache, but we haven't tried |
| **IRQ and DMA trace** | When the device raised its interrupt, when the guest acknowledged it, what DMA moved | Hooks in `picint`/`picintc` and the DMA controller |
| **Finer steps than 1 ms** | Timing-sensitive device behaviour: a FIFO draining, a reset pulse | The automation socket steps whole 1 ms quanta, because the main loop runs `cpu_exec` 1 ms at a time. A `step cycles N` command could run part of a quantum. Instruction steps exist through the stub. "One bus clock" has no meaning in 86Box: the CPU runs instruction by instruction, and devices run off timers on its clock |
| **Unplugging the network cable** | Exercise a driver's link-change handling | 86Box has a link state per card, read from the config and polled every tick (`network_rx_queue`), but nothing changes it at run time. A small automation command could. Of the emulated cards, the 3Com 3C509B and 3C59x report it to the guest |
| **Hot-plugging devices** | Insert or remove hardware under a running OS | 86Box sets up devices at power-on and hard reset, so the rest is a reconfigure and reset: `vm_stop`, edit `86box.cfg`, `vm_start`. Removable media already change at run time (floppy, CD, ZIP, MO, cassette, cartridge). There's no PCMCIA or CardBus socket to insert cards into, and the USB host controllers are registers with no devices behind them |
| **PCI config writes** | Try BAR or interrupt routing changes by hand | Possible now with `io_write` to 0xCF8/0xCFC; a dedicated tool would be a few lines |
| **Snapshots and rewind** | Return to just before a bug, again and again | 86Box has no savestates; this would be a large change. Held time with a repeatable boot is the substitute: a run driven the same way repeats exactly |
| **Symbols** | Name addresses in a driver | A map or symbol file loaded by the MCP server; for Windows drivers it also needs the load address, which V9x could find from inside the guest |
| **A debug console port** | A guest driver prints with one `OUT`, like Bochs's port 0xE9 | 86Box has no such device. COM1 capture is the current way |
