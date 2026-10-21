#!/bin/sh
# Status-line tap: saves the status-line JSON per session, then runs the original
# status-line command with the same stdin. Meant to be SOURCED (`. tap`) by the shell
# Claude Code already starts for statusLine.command, so it adds no process of its own
# except one `mv` for the atomic write. Works in sh and zsh; does not use $0; variables
# are namespaced. Prints nothing of its own and always ends with exit 0 (it ends the
# shell Claude Code started for this one command).
exec 2>/dev/null
__prorab_nl='
'
__prorab_in=
while IFS= read -r __prorab_l; do __prorab_in=$__prorab_in$__prorab_l$__prorab_nl; done
[ -n "$__prorab_l" ] && __prorab_in=$__prorab_in$__prorab_l

__prorab_cache=${PRORAB_CACHE:-$HOME/.cache/prorab}
__prorab_src=
__prorab_rest=${__prorab_in#*\"session_id\"}
if [ "$__prorab_rest" != "$__prorab_in" ]; then
  __prorab_rest=${__prorab_rest#*:}
  __prorab_rest=${__prorab_rest#"${__prorab_rest%%[! 	]*}"}
  case $__prorab_rest in
    \"*)
      __prorab_rest=${__prorab_rest#\"}
      __prorab_sid=${__prorab_rest%%\"*}
      case $__prorab_sid in
        "" | *[!A-Za-z0-9_-]*) ;;
        *)
          __prorab_dir=$__prorab_cache/usage
          __prorab_tmp=$__prorab_dir/.tmp.$$
          if { [ -d "$__prorab_dir" ] || mkdir -p "$__prorab_dir"; } &&
            printf '%s' "$__prorab_in" >"$__prorab_tmp" &&
            mv -f "$__prorab_tmp" "$__prorab_dir/$__prorab_sid.json"; then
            __prorab_src=$__prorab_dir/$__prorab_sid.json
          else
            rm -f "$__prorab_tmp"
          fi
          ;;
      esac
      ;;
  esac
fi

__prorab_orig=
if [ -r "$__prorab_cache/statusline.orig" ]; then
  while IFS= read -r __prorab_l; do __prorab_orig=$__prorab_orig$__prorab_l$__prorab_nl; done <"$__prorab_cache/statusline.orig"
  [ -n "$__prorab_l" ] && __prorab_orig=$__prorab_orig$__prorab_l
fi
case $__prorab_orig in
  *[!\ \	$__prorab_nl]*)
    if [ -n "$__prorab_src" ]; then
      (eval "$__prorab_orig") <"$__prorab_src"
    else
      printf '%s' "$__prorab_in" | (eval "$__prorab_orig")
    fi
    ;;
esac
exit 0
