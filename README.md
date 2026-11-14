# tmux-program-status

Show what each pane's program is doing. The plugin registers one tmux format,
`#{E:@program-status}`, and adds nothing to your status lines until you place it.

| Pane state | Badge |
| --- | --- |
| OSC 7501 `working`, app `claude`, message `Building project` | `[working claude: Building project]` |
| OSC 7501 `idle` / `done` / `blocked` / `error` | `[idle]`, `[done]`, `[blocked]`, `[error]` |
| OSC 9;4 `normal` 55%, or 100% | `[working 55%]`, `[working 100%]` |
| OSC 9;4 `indeterminate` | `[working]` |
| OSC 9;4 `paused` 20% | `[paused 20%]` |
| OSC 9;4 `error` 7% | `[error 7%]` |
| OSC 9;4 `hidden`, or no report | empty |

The plugin writes the global option `@program-status`. It does not change hooks,
key bindings, `pipe-pane`, other options, or your shell.

## Requirements

tmux must report these format fields:

- `pane_program_state`, `pane_program_name`, `pane_program_message` (OSC 7501).
  Landed upstream 2026-11-12 in
  [434ab3a2](https://github.com/tmux/tmux/commit/434ab3a25072837616c34f6d6256f82f52ed988b).
- `pane_pb_state`, `pane_pb_progress` (OSC 9;4 progress). Landed upstream
  2026-04-03 in
  [bdd78ce3](https://github.com/tmux/tmux/commit/bdd78ce38e8708006f87c424cd93437db2881abc).

An older tmux lacks some or all of them. A missing field expands to nothing, so
`#{E:@program-status}` draws an empty badge and the rest of your status line
works. The plugin reads fields, not a version number, and sets no minimum tmux
version. Test your tmux with `tmux display-message -p '#{pane_program_state}'`
after running a program that reports status.

The runtime needs Bash 3.2 or newer and tmux.

## Install with TPM

```tmux
# add this line before the existing TPM initialization
set -g @plugin 'cosgroveb/tmux-program-status'

set -g status-right '#[fg=yellow]#{E:@program-status} %H:%M '

# existing TPM initialization
run '~/.tmux/plugins/tpm/tpm'
```

Press your TPM install key (prefix + I by default) to fetch the plugin, then
reload your configuration (prefix + r in TPM's default binding) or restart tmux.

## Install manually

```sh
git clone https://github.com/cosgroveb/tmux-program-status \
  ~/.tmux/plugins/tmux-program-status
```

Then in `~/.tmux.conf`:

```tmux
run-shell '~/.tmux/plugins/tmux-program-status/program-status.tmux'
```

Run `tmux source-file ~/.tmux.conf` after editing. `program-status.tmux` calls
`tmux set-option -g` on the running server. Set `TMUX_PROGRAM_STATUS_TMUX` to
another tmux path before running it if `tmux` on your `PATH` is not the server
you configured.

## Display the badge

Add the format where you want it. The braces around it are part of the format,
not documentation:

```tmux
set -g status-right '#[fg=yellow]#{E:@program-status} %H:%M '
setw -g window-status-format '#I:#W#{E:@program-status}'
setw -g pane-border-format '#{pane_index} #{E:@program-status}'
set -g pane-border-status top   # only if you want pane borders
```

`#{E:...}` expands the option's value a second time, so the per-pane fields are
read when tmux draws, for that pane. The same format works in `status-left`,
`window-status-current-format`, and `status-format`.

Two consequences of tmux's own refresh:

- A new report appears on the next status redraw, which follows your
  `status-interval`. Set `status-interval 1` for quicker updates.
- tmux owns report lifetime. The plugin renders whatever fields tmux stores,
  so a badge can stay after the reporting program exits. A native `done` or
  `error` may persist until the program sends `clear` or tmux clears it.

## Precedence

tmux stores the two protocols separately, and each pane has at most one badge.

1. If `pane_program_state` is non-empty (an OSC 7501 report is current), the
   native badge wins. App and message come from the same OSC 7501 report. A
   legacy percentage is never attached to a native state.
2. Otherwise the OSC 9;4 fields are used: `pane_pb_state` and
   `pane_pb_progress`. `normal` stays `working` at 100%, and `paused` stays
   `paused`. `hidden` and an empty state draw nothing.
3. If neither is set, the badge is empty.

OSC 7501 `clear` removes the native state. If an older OSC 9;4 report is still
stored for the pane, the badge falls back to it. This is current state, not a
history: the badge shows whatever tmux holds at draw time.

Some tmux builds that carry OSC 7501 keep a `done` or `error` state after the
command finishes. In those builds a leftover native state also wins over a later
OSC 9;4 report until the native state is cleared. The badge follows what the
fields say.

Application text is passed through `#{q/h:...}`, which doubles `#`. App names
and messages that contain `#[...]`, `#{...}`, or `#(...)` appear as text.
tmux itself rewrites `#(` to `_(` in pane data and that rewrite is visible in
the badge. No app text opens a style, a nested format, or a command.

## Uninstall

For a TPM install, remove this plugin's `@plugin` line and keep the shared
`run '~/.tmux/plugins/tpm/tpm'` line so TPM and your other plugins still load.
For a manual install, remove only this plugin's `run-shell` line.

Then remove every `#{E:@program-status}` reference and reload. To clear the
option from the running server:

```sh
tmux set-option -gu @program-status
```

Deleting `~/.tmux/plugins/tmux-program-status` is safe once no line references
it.

## Limits

Each badge shows one pane's current state. There is no history, no counter, and
no display outside tmux. Programs that report neither protocol draw nothing.
The plugin does not send status reports, query programs, or run anything in the
background.

## Development

```sh
python3 tests/integration.py                  # tmux from PATH
python3 tests/integration.py /path/to/tmux    # or set TMUX_BIN
shellcheck program-status.tmux
```

Linux and macOS with Python 3. The suite needs a tmux built from source with
both protocols to exercise every state, and a tmux without them to exercise the
empty badge. `tests/integration.py` probes the fields and skips cases the binary
cannot support.
