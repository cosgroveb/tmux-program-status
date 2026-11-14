#!/usr/bin/env bash
# tmux-program-status: register the #{E:@program-status} format.
#
# Native OSC 7501 state (pane_program_state) wins while it is set. Otherwise
# the OSC 9;4 progress bar (pane_pb_state) is shown. Builds without either
# field expand the format to nothing. Application text passes through
# #{q/h:...}, which doubles # so app data cannot open a format or a style.
# Progress badges emit the percent as #{a:37} (literal %), not a raw "%":
# E re-expands through strftime, and Darwin's strftime drops unknown "%]"
# (while glibc keeps it); a:37 runs after time expansion so it stays a %.

set -euo pipefail

tmux=${TMUX_PROGRAM_STATUS_TMUX:-tmux}

format='#{?pane_program_state,[#{pane_program_state}#{?pane_program_name, #{q/h:pane_program_name}}#{?pane_program_message,: #{q/h:pane_program_message}}],#{?#{==:#{pane_pb_state},normal},[working #{pane_pb_progress}#{a:37}],#{?#{==:#{pane_pb_state},indeterminate},[working],#{?#{==:#{pane_pb_state},paused},[paused #{pane_pb_progress}#{a:37}],#{?#{==:#{pane_pb_state},error},[error #{pane_pb_progress}#{a:37}],}}}}}'

"$tmux" set-option -g @program-status "$format"
