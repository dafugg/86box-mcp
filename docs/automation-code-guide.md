# A guide to the 86Box automation code

The automation socket was merged into 86Box in
[#8365](https://github.com/86Box/86Box/pull/8365), as two commits. This guide
covers what each part does, why it's built that way, and the questions people
ask about it. Read it with the code open.

```sh
git -C 86box show --stat 96067f9f1 1b3985523   # the mouse flag, then the socket
```

## The idea in one paragraph

A client connects to a socket and sends one line of text per request, and
86Box answers each with one line of JSON. Requests don't run on the socket
thread. They're handed to the **CPU thread**, which runs them between two
`pc_run()` calls, so every command sees the machine between two emulation
quanta, never halfway through one. `hold` stops the main loop from calling
`pc_run()`. `step N` runs exactly N quanta of 1 ms (10 ms with `force_10ms`)
as fast as the host allows, then holds again. That is the whole trick behind
"held time": the guest can't tell how long the client spends thinking.

## Map of the change

| Where | Lines | What to understand |
|---|---|---|
| `mouse.c`, `mouse.h`, `mouse_{ps2,serial,bus}.c` | 9 | **Commit 1:** a `mouse_automation` flag the mice accept alongside `mouse_capture` |
| `CMakeLists.txt`, `src/CMakeLists.txt` | 9 | **Commit 2 from here:** `-D AUTOMATION=ON` adds `automation.c`, defines `USE_AUTOMATION` and links `ws2_32` on Windows (copied from `GDBSTUB`) |
| `include/86box/automation.h` | 63 | The API; with the option off, every hook is an empty macro |
| `86box.c` | 27 | Four command-line options; `automation_init()` at the end of `pc_init_modules()`, `automation_close()` in `pc_close()` |
| `qt/qt_main.cpp`, `unix/sdl_main.c` | 18 | The main-loop hooks (below); **the core of the review** |
| `video/video.c` | 3 | `automation_frame()` sees every finished frame |
| `cpu/x86.c` | 2 | `automation_reset()` counts resets |
| `automation.c` | 1290 | Everything else |
| `doc/specifications/automation.md` | 110 | The protocol, next to `86box-unit-tester.md` |

## The main-loop hooks (read these first)

In `main_thread_fn()` (Qt; SDL's `main_thread()` is the same idea):

```c
automation_poll();                          // run a pending request, if any
const int automation = automation_run_state();
if (((debt_ns >= quantum_ns) || fast_forward || (automation == AUTOMATION_RUN))
    && !dopause && (automation != AUTOMATION_HOLD)) {
    pc_run();
    automation_quantum();                   // count 1 ms; finish step/wait/key
    ...
    if (automation == AUTOMATION_RUN)
        debt_ns = 0;
```

- **`automation_run_state()`** returns one of three answers. `HOLD` means
  don't run. `RUN` means run now, without pacing (a `step` or `wait` is in
  progress). `FREE` means "no opinion": normal real-time pacing. Without
  `--automation`, it's always `FREE` (a macro when not compiled in), so
  default builds behave exactly as before.
- **`debt_ns = 0` after a `RUN` quantum:** the real-time pacer counts how far
  emulation is behind the wall clock. Unpaced steps would otherwise build up
  "debt", and the machine would race to catch up the moment you `release` it.
- **`dopause` still wins.** The UI's pause stops everything, steps included.
  Whether `step` should override the pause is still open: the PR asked, and
  the merge didn't settle it.

## `automation.c`, by section

**Threads and the mailbox (`automation_thread`, `automation_poll`,
`complete`).** The listener thread takes one client at a time. For each line
it copies the request into `req_line`, sets `req_pending` (an atomic), and
waits on the event `req_done`. On the CPU thread, `automation_poll()` sees
the flag and calls `handle_request()`. That writes the reply into `resp` and
calls `complete()`, which clears the flag and sets the event. Only one
request is ever in flight, so `resp` needs no lock. The listener only reads
it after the event.

**Commands that take emulated time (`begin_timed`, `end_timed`,
`automation_quantum`).** `step`, `wait text`, `wait mem` and `key` don't
reply at once. They:
1. note whether the machine was held;
2. let it run;
3. return without calling `complete()`.

`automation_quantum()` runs after every `pc_run()` and checks the command.
For `step`, has the deadline passed? For `wait`, has the condition become
true, or did it time out? For `key`, is the next queued key event due?
`end_timed()` then restores the hold state and replies. While a timed
command runs, `automation_poll()` takes no new requests.

**Keys (`KEY_GAP_MS`).** `key down|up <scancode>...` queues events and
delivers them 2 ms of emulated time apart, replying after the last one.
**Why:** one key can be up to six bytes in scan-code set 2 (an extended key
with fake shifts). Sending several at the same instant overflows the
keyboard controller's 16-byte FIFO, and keys get lost silently. That really
happened during testing.

**Mouse.** `mouse <dx> <dy> <buttons>` sets `mouse_automation = 1`, then
uses the normal `mouse_scale()` and `mouse_set_buttons_ex()`. **Why not
`mouse_capture`:** setting it made a visible window hide the host cursor and
start forwarding host motion. That's the reason for the separate first
commit.

**Frames (`automation_frame`, `encode_png`, `record_frame`).** The hook in
`video_blit_memtoscreen_monitor()` copies monitor 0's finished frame into
`frame_buf`, under `frame_mutex`. It runs where the video card blits, which
is normally the CPU thread; the mutex covers cards that blit from a thread of
their own. A screenshot is taken from that copy, not the window,
so headless runs work and the image doesn't depend on the renderer. If
`record` is on, every N ms of emulated time the frame is also written as a
PNG named after the emulated ms. That write happens in the frame hook, normally
on the CPU thread, so a slow disk slows emulation a little while recording.
- `screenshot <path>` writes a file.
- A bare `screenshot` returns the PNG base64-encoded in the reply, which
  works over TCP from another machine.

**Text screen (`text_info`, `cmd_text`, `text_contains`).** These read the
BIOS data area at 0x449 onwards: video mode, columns, rows, page and cursor.
Then they read the text buffer at B800:0, or B000:0 for mode 7. That only
works in BIOS text modes, and the reply says when the screen isn't in one.
`wait text` only matches within a single row.

**Memory, I/O and registers.** `memr` and `memw` go through
`mem_view_read()` and `mem_view_write()` in `mem.c`: the bus's mapping tables
by default (the path the devices use), the CPU's (`cpu`), or for the SMM state
the CPU isn't in, a walk of the mapping list (`normal`, `smm`). They call the
mapping's handlers, splitting a word or dword the mapping can't take whole.
`linear` translates each page with `mmutranslate_noabrt()` under
`cpl_override`, then reads the CPU's view. `ior`/`iow` call `inb()`/`outb()`
and their wider forms; `pcir`/`pciw` call the card's configuration handlers
through `pci_config_read()`/`pci_config_write()` in `pci.c`, so 0xCF8 is left
alone. All of them restore `cycles`, so nothing is charged to the guest.
`regs` reads `cpu_state`.

**Media and reset.** `fdd load|eject` and `cd load|eject` copy what the UI's
menu handlers do, including the status-bar icon update. `reset hard` calls
`pc_reset_hard()`, `reset cad` calls `pc_send_cad()`, and `quit` replies and
then calls `plat_power_off()`. `quit soft` presses the power button instead:
ACPI's if the machine has it, else the board's own, else it powers off as
`quit` does. The guest only sees the button while the CPU runs, so the client
releases the machine right after pressing it.

**Fast forward.** `ff on|off` calls `plat_fast_forward()`, which sets the
same `fast_forward` the menu toggles (the main-loop test above) and keeps the
menu's check in step. It only matters when the machine is released: held
steps are unpaced already.

**`info`** reports the machine, CPU, memory, video, sound, disks, media and
reset counts. It also gives emulation speed, measured over each emulated
second, with wall time spent held excluded.

**Transports and security (`parse_endpoint`, `listen_unix`, `listen_tcp`,
`make_token`, `token_matches`).**
- **The UNIX socket** is created owner-only: `umask(077)` around `bind()`,
  so it never has the default permissions, even briefly.
- **TCP** refuses non-loopback addresses unless `--automation-allow-remote`
  is given.
- **The token:** 32 bytes from `/dev/urandom` (`rand_s()` on Windows),
  hex-encoded, written to a 0600 file. The first line on a TCP connection
  must be `auth <token>`, compared in constant time.
- **The log** shows only the token's first 8 characters.
- **`automation_close()`** removes the socket file and the token file.

## Questions people ask about it

- **"Why not a Qt-only feature?"** The core is plain C with the platform's
  thread and socket calls, so both frontends work and headless runs need no
  window system. The frontend hooks are a few lines each.
- **"What does it cost when it's off?"** Nothing. With `AUTOMATION=OFF`
  every hook is an empty macro and `automation.c` isn't compiled.
  `automation_run_state()` becomes the constant `AUTOMATION_FREE`.
- **"What does it cost when it's on but idle?"** A frame copy per blit
  (`memcpy` of monitor 0) and a poll of an atomic per main-loop pass.
- **"Is the socket safe?"** The UNIX socket is owner-only from the moment
  it exists; TCP is loopback plus a token by default. A client with access
  controls the whole machine, memory included, and the docs say so plainly.
  `umask()` is process-wide, but it's only changed for the one `bind()`
  call at startup.
- **"Does stepping stay exact?"** Each `pc_run()` is one quantum, `emu_ms`
  counts quanta, and the pacer's debt is cleared. Two cold boots with the
  same inputs give byte-identical frames at the same emulated time. 86box-mcp
  tests this.
- **"Why one client at a time?"** It keeps the mailbox simple and the order
  of commands well defined. A second client waits in `listen()`'s backlog.

## What has and hasn't been tested

- **Tested:**
  - **Linux, Qt frontend, headless and windowed:** the 86box-mcp test suite,
    an end-to-end demo with 34 checks, OS installs (MS-DOS 6.22, FreeDOS 1.4,
    Windows for Workgroups 3.11), and the Solitaire bot.
  - **Windows:** a MinGW cross-build, driven over TCP from Linux.
  - **CI:** the upstream MSYS2 workflow, with `AUTOMATION=OFF` and `ON`.
- **Not tested:**
  - the SDL frontend's hook;
  - an MSYS2-built binary actually run on Windows;
  - macOS.
