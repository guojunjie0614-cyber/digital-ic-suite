#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PY="$DIR/eda_env.py"
PREFLIGHT_PY="$DIR/eda_preflight.py"

usage() {
  cat <<'EOF'
Usage: eda_exec.sh [--config <tools.yaml>] [--target <name>] <tool> --preflight [quick|probe|smoke]
       eda_exec.sh [--config <tools.yaml>] [--target <name>] <tool> --probe
       eda_exec.sh [--config <tools.yaml>] [--target <name>] <tool> --exec <tool-args...>
       eda_exec.sh [--config <tools.yaml>] [--target <name>] <tool> --cmd '<remote-shell>'
       eda_exec.sh [--config <tools.yaml>] [--target <name>] <tool> --cmd-file <local-script>
EOF
}

cfg_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --config) [[ $# -ge 2 ]] || { usage >&2; exit 1; }; cfg_args+=(--config "$2"); shift 2 ;;
    --target) [[ $# -ge 2 ]] || { usage >&2; exit 1; }; cfg_args+=(--target "$2"); shift 2 ;;
    --) shift; break ;;
    -*) usage >&2; exit 1 ;;
    *) break ;;
  esac
done

[[ $# -ge 2 ]] || { usage >&2; exit 1; }
tool="$1"; mode="$2"; shift 2

if [[ "$mode" == --preflight ]]; then
  exec python3 "$PREFLIGHT_PY" "${cfg_args[@]+"${cfg_args[@]}"}" "$tool" --level "${1:-quick}"
fi

eval "$(python3 "$ENV_PY" "${cfg_args[@]+"${cfg_args[@]}"}" shell-vars "$tool")"
eval "transport=($TRANSPORT_CMD)"

quote_exec() {
  local out="" arg esc
  for arg in "$TOOL_BIN" "$@"; do
    printf -v esc '%q' "$arg"
    out+="${out:+ }$esc"
  done
  printf '%s' "$out"
}

case "$mode" in
  --probe)    [[ $# -eq 0 ]] || { usage >&2; exit 1; }; body="$PROBE_CMD" ;;
  --exec)     [[ $# -gt 0 ]] || { usage >&2; exit 1; }; body="$(quote_exec "$@")" ;;
  --cmd)      [[ $# -eq 1 ]] || { usage >&2; exit 1; }; body="$1" ;;
  --cmd-file) [[ $# -eq 1 && -f "$1" ]] || { usage >&2; exit 1; }; body="$(<"$1")" ;;
  *) usage >&2; exit 1 ;;
esac

# Run environment setup + inline preflight + tool command in a SINGLE SSH session.
if [[ "${EDA_SKIP_PREFLIGHT:-0}" == 1 ]]; then
  remote_script="$BASE_REMOTE; $body"
else
  remote_script="$BASE_REMOTE; $INLINE_PREFLIGHT; $body"
fi

wrap_bash() {
  local b64
  b64="$(printf '%s' "$1" | base64 | tr -d '\n')"
  printf "bash -c 'eval \"\$(printf %%s %s | (base64 -d 2>/dev/null || base64 -D))\"'" "$b64"
}

set +e
"${transport[@]}" "$(wrap_bash "$remote_script")"
rc=$?
set -e
[[ $rc -eq 0 ]] || echo "EDA COMMAND FAILED: $tool@$TARGET_NAME exited $rc" >&2
exit "$rc"
