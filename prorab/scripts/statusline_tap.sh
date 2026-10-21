#!/bin/sh
# Status-line tap: saves the status-line JSON per session, then runs the original
# status-line command with the same stdin. Only builtins where possible (one `mv`); the original command runs in a subshell via eval (no extra exec).
# Prints nothing of its own and always exits 0.
exec 2>/dev/null
NL='
'
in=
while IFS= read -r l; do in=$in$l$NL; done
[ -n "$l" ] && in=$in$l

cache=${PRORAB_CACHE:-$HOME/.cache/prorab}
src=
rest=${in#*\"session_id\"}
if [ "$rest" != "$in" ]; then
  rest=${rest#*:}
  rest=${rest#"${rest%%[! 	]*}"}
  case $rest in
    \"*)
      rest=${rest#\"}
      sid=${rest%%\"*}
      case $sid in
        "" | *[!A-Za-z0-9_-]*) ;;
        *)
          dir=$cache/usage
          tmp=$dir/.tmp.$$
          if { [ -d "$dir" ] || mkdir -p "$dir"; } &&
            printf '%s' "$in" >"$tmp" &&
            mv -f "$tmp" "$dir/$sid.json"; then
            src=$dir/$sid.json
          else
            rm -f "$tmp"
          fi
          ;;
      esac
      ;;
  esac
fi

orig=
if [ -r "$cache/statusline.orig" ]; then
  while IFS= read -r l; do orig=$orig$l$NL; done <"$cache/statusline.orig"
  [ -n "$l" ] && orig=$orig$l
fi
case $orig in
  *[!\ \	$NL]*)
    if [ -n "$src" ]; then
      (eval "$orig") <"$src"
    else
      printf '%s' "$in" | (eval "$orig")
    fi
    ;;
esac
exit 0
