---
name: 86box
description: How to drive an 86Box PC emulator through the 86box MCP server (box86mcp) - booting VMs, held versus real-time running, input, reading the screen and memory, installing software, playing real-time programs turn by turn, comparing 86Box builds and measuring speed, the GDB-stub debugger, several VMs in lockstep (multiplayer over 86Box's network switch, side-by-side videos), and working alongside V9x Remote Agent. Use it before calling the 86box server's tools (vm_start, step, act, screen_text, measure_speed, breakpoint_set, step_all...), or when planning tests, benchmarks or automation on a vintage PC in 86Box.
---

# Driving 86Box through the 86box MCP server

The server controls a PC emulated by 86Box at the hardware level: the emulated keyboard and mouse,
frames from the emulated video card, physical memory, media, resets. Nothing runs inside the
guest, so it works from BIOS POST on, under any OS. One server can run several VMs, named with `vm=`.

## The time model: decide this first

- **Held (the default).** Emulated time advances only inside tool calls that run it: `step(ms)`,
  `act`, `press_keys`, `type_text`, `wait_for_text`, `wait_for_memory`, `dos_run`. Between calls the
  machine is frozen. Think as long as you like, and the same calls give the same result every time.
  Held steps run as fast as the host allows, often faster than real time. With several VMs, time
  passing on one held VM passes on every held VM in its group (all of them, by default): see
  "Multiplayer, or several VMs" below.
- **Real time.** `vm_start(held=False)`, or `release` at any point; `hold` goes back.
  `fast_forward(True)` lets a released machine run as fast as the host allows, and
  `fast_forward(False)` returns it to real-time pacing. Use it when
  something must run on its own: a person watching or typing, a server or agent inside the guest
  (V9x Remote Agent, network services), or real-time behaviour you want to observe. Tools that
  advance time then just wait for wall time to pass.

Pick held for tests, games, installs and anything you want repeatable. Pick real time only for
the cases above, and say why.

## Starting and stopping

- `vm_create(vm_dir, ...)` makes a VM with a blank disk (a 486DX2-66, or a copy of any
  `86box.cfg` as `config`, with a saved CMOS as `nvr`). `dos_create_vm` makes one with DOS on it.
- `vm_start()` boots. Without `vm_dir` it takes the VM this session last started, then
  `$BOX86_VM_DIR`, then the last VM any session started. `held`, `window` (the 86Box window; on
  by default on Windows), `debugger`, and `binary` (see below).
- `vm_stop()` powers off. `vm_stop(soft=True)` presses the power button and lets an ACPI guest
  (Windows 98) shut down first.
- `vm_status` (time, frames, held), `vm_info` (machine, CPU, RAM, cards, media, reset counts,
  `speed_percent`). A `soft_resets` count that rises on its own usually means a crash or triple
  fault.
- A tool that fails says why. "no VM running" means call `vm_start`.

## Seeing and typing

- **Text modes:** `screen_text` (exact, cheap), `wait_for_text(text, timeout_ms)`.
- **Graphics:** `screenshot`. Compare frames rather than guessing from one.
- **Keyboard:** `type_text("dir\n")`, `press_keys(["ctrl", "alt", "del"])`, or `act(hold_keys, ms)`
  to hold keys for an exact time and get the frame at the end: one turn of a real-time game.
- **Mouse:** relative (`mouse(dx, dy)`); `mouse(left=True, release=False)` keeps the button down
  (a dropdown that picks on release), and the next `mouse` call lets go. `mouse_to(x, y)` closes the
  loop by finding the pointer on screen, or in memory if the program publishes it; `mouse_drag` for
  drags. The hotspot is the pointer's tip (top-left of an arrow or a pointing finger).
- **DOS pointing:** with MOUSEBOX.COM loaded after the mouse driver (examples/touch), `mouse_to` puts
  the pointer there directly, for every way programs read the mouse, with no screen search or
  corner parking; then click with `mouse(left=True)`. `mouse_state` shows how the program reads it.
- **Touch:** `tablet(x, y)` touches at a screenshot pixel, if the VM has 86Box's MicroTouch
  touchscreen and a touch driver is loaded in the guest (examples/touch); otherwise it fails or does
  nothing. Before the driver loads (BIOS, boot menus) use `mouse`. Some programs ignore touches:
  docs/input-quirks.md in the 86box-mcp repo lists how programs read input, and their oddities.
- **Several VMs at once:** `together([{"vm": "p1", "keys": [...]}, {"vm": "p2", "tablet": [x, y]}, ...])`
  gives each its input at the same emulated instant.
- **Sound** (86Box with the audio capture): you can't hear it, so measure it. `sound_level(ms)` per
  stream (main = sound cards' digital audio, music = OPL and PC speaker, cd, midi...);
  `wait_for_sound(threshold_db)` / `wait_for_silence(threshold_db, for_ms)` to run until a sound
  starts or ends; `sound_spectrogram(ms)` to see whether it's music, a tone or noise;
  `sound_save(path)` for the person to listen to. `record_start` records sound with the frames, and
  `record_stop(video=...)` puts it in the video; `sound_check()` looks for dropouts. `sync=True` on
  `record_stop`/`record_stop_all` overlays each frame's CPU cycles, sample position and
  picture/sound hashes, to show VMs or picture and sound staying in step. A sound card
  holding a DC level after playback shows as a constant level (peak = RMS), not sound.
- **DOS:** `dos_run("command")` types it and waits for the prompt. `dos_install_files` copies onto C:
  while the VM is stopped; `dos_put_floppy` or `cdrom_insert(files=[...])` while it runs.

## Memory and state

`read_memory`, `write_memory`, `wait_for_memory`, `registers` (also CR2-CR4, DR0-DR7, GDTR/IDTR/LDTR/TR and segment caches; `fpu_registers` for x87/MMX). In protected mode, `descriptor_table("gdt"|"ldt"|"idt")` decodes the tables, `translate_address(offset, selector)` walks selector -> linear -> physical with the page-table entries, and `read_memory(addr, linear=True)` reads through paging; breakpoint addresses are linear. To find a game's variable:
`snapshot_memory`, make it change (lose a life, score), `diff_memory("decreased")`, repeat until a
few addresses are left. `find_memory(text=...)` finds strings, for example text a program is
showing.

## Workflows

**Install an OS or a program.** Insert media (`floppy_insert`, `cdrom_insert`), boot held,
`wait_for_text` or `screenshot` at each step, answer with `press_keys`/`type_text`. Hold through
the whole install so nothing times out between your moves. `record_start(dir)` before booting, and
`record_stop(video=...)` after, give a captioned video of the whole thing.

**Play a real-time game.** Held, one `act` per decision: hold the keys for the ms you mean, look at
the returned frame, decide. Read the game's state from memory once you've found it.

**Compare 86Box builds, or settings.** For each build: a fresh copy of the same VM, then
`vm_start(vm_dir=copy, binary=build)`, the same steps to the same point, `measure_speed(10,
paced=False)` a few times, `vm_stop`. Unpaced measures throughput, uncapped: that's the number
that moves when a build gets slower or faster. `paced=True` only says whether the build keeps up
with real time (it can't read above 100 %). Compare on one host, with nothing else heavy running.
For a full release benchmark (Windows 98, 3DMark 99, a Voodoo 2), use `bench/` in the 86box-mcp
repo instead. Note that the emulated Voodoo runs as fast as the host renders, so its 3D results
measure the host renderer.

**Debug a driver or device emulation.** `vm_start(debugger=True)`, with 86Box built with
`-D GDBSTUB=ON`. Then:
- `breakpoint_set(addr)` for code, or `breakpoint_set(addr, "write"/"read"/"access", length)` for
  data. Addresses are linear: segment × 16 + offset in real mode.
- `run_until_break`, `step_instructions(n)`.
- `io_read`/`io_write` for device registers, `pci_scan`/`pci_config_read`/`pci_config_write`
  (these don't need the debugger on an 86Box with the socket's `ior`/`pcir` commands).
- `read_memory`/`write_memory` reach the whole 4 GB: `width=4` for MMIO registers, `view="smm"`
  for SMRAM, `view="cpu"` for what the CPU sees now, `view="linear"` through the page tables.

While the debugger has the CPU stopped, tools that need time fail, saying so: call `debug_continue`
(or `run_until_break`) first. A breakpoint hit during another tool's call stops the CPU, and that
call reports where.

**Multiplayer, or several VMs.** Name each VM: `vm_start(vm="p1", vm_dir=...)`, then `vm="p1"` on
any tool. Held VMs in a group step together: any tool that passes time on one (`step`, `mouse`,
`mouse_to`, `press_keys`, `wait_for_text`...) passes the same time on all, in 10 ms lockstep quanta,
so a networked game never sees a peer freeze and drop it. Every VM starts in group "default"; put
an unrelated VM in its own group (`vm_group("solo", vm=...)` or `vm_start(group=...)`) so it runs
independently. `step_all(ms)` runs the group with no input; `vm_list` shows each VM's time. For a
side-by-side video, `record_start` on each, then `record_stop_all(video=..., speed=["0:4",
"75000:1"])`: frames are paired by emulated time, with the clock shown. For 86Box's network switch,
give the VMs `net_01_net_type = nlswitch` with one `net_01_secret`, and no `mac` lines (copied VMs
would share a MAC).

**Act like a person with several VMs.** Input given to several networked VMs in the same instant
(seven players clicking Ready together) can trip latent bugs a person never would: in Warcraft II
it dropped a player twice. Stagger such input by a second or so unless simultaneity is the point.
And don't expect exact replays across VMs: network packets travel in host time, so their order
between VMs can differ from run to run. Judge such runs by outcome (who joined, who won, where
units ended up), not by frames.

**Work with V9x Remote Agent (Windows 9x).** V9x runs inside the guest and needs emulated time to
pass. `release` before V9x calls, or send them while a `step` runs. No more than 4 V9x calls at
once. After V9x calls have timed out while the machine was held, ping until it answers before
the next real call. Use V9x for files, programs and text; use 86box-mcp for time, frames, memory,
media and resets.

## Pitfalls

- `screen_text` and `wait_for_text` only work in text modes.
- The disk tools (`dos_install_files`, `dos_create_vm`) need the VM stopped.
- Held means nothing in the guest runs between calls: no network replies, no background programs.
  If something "hangs", check whether you are waiting on the guest while it's held.
- Mouse movement is relative, and guests accelerate it: use `mouse_to` for positions. But
  `mouse_to` first parks the pointer in a corner, which scrolls the map in an RTS or turns the view
  in a 3D game: there, nudge with `mouse(dx, dy)` and check with a screenshot.
- A new `vm_start` for a VM name stops that VM's previous machine; other names are unaffected.
