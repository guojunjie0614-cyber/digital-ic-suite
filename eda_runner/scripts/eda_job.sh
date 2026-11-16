#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_PY="$DIR/eda_env.py"
EXEC_SH="$DIR/eda_exec.sh"

usage() {
  cat <<'EOF'
Usage: eda_job.sh [--config <tools.yaml>] [--target <name>] init <job> [--reuse]
       eda_job.sh [--config <tools.yaml>] [--target <name>] sync <job> <local-path> [remote-subdir]
       eda_job.sh [--config <tools.yaml>] [--target <name>] run <job> <tool> --probe|--exec ...|--cmd '...'|--cmd-file <file>
       eda_job.sh [--config <tools.yaml>] [--target <name>] fetch <job> <local-dir> [remote-subdir]
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

[[ $# -ge 1 ]] || { usage >&2; exit 1; }

valid_component() {
  [[ "$2" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ && "$2" != . && "$2" != .. ]] || {
    echo "Invalid $1: $2" >&2
    return 1
  }
}

valid_subdir() {
  [[ -n "$1" && "$1" != /* && "$1" != */ && "$1" != *//* ]] || return 1
  local part parts=()
  IFS=/ read -r -a parts <<<"$1"
  for part in "${parts[@]}"; do valid_component "path component" "$part" || return 1; done
}

q() { printf '%q' "$1"; }

wrap_bash() {
  local b64
  b64="$(printf '%s' "$1" | base64 | tr -d '\n')"
  printf "bash -c 'eval \"\$(printf %%s %s | (base64 -d 2>/dev/null || base64 -D))\"'" "$b64"
}

# Validate path-safety arguments locally BEFORE resolving transport/config where possible.
action="$1"; shift
case "$action" in
  init|sync|run|fetch)
    [[ $# -ge 1 ]] || { usage >&2; exit 1; }
    valid_component "job name" "$1"
    ;;
  *) usage >&2; exit 1 ;;
esac

eval "$(python3 "$ENV_PY" "${cfg_args[@]+"${cfg_args[@]}"}" target-vars)"
eval "transport=($TRANSPORT_CMD)"
job_path() { printf '%s/%s' "$REMOTE_RUN_ROOT" "$1"; }

case "$action" in
  init)
    [[ $# -ge 1 && $# -le 2 ]] || { usage >&2; exit 1; }
    dir="$(job_path "$1")"; reuse="${2:-}"
    [[ -z "$reuse" || "$reuse" == --reuse ]] || { usage >&2; exit 1; }
    if [[ "$reuse" == --reuse ]]; then
      init_script="mkdir -p $(q "$dir")/{src,reports,logs,out}"
    else
      init_script="mkdir -p $(q "$REMOTE_RUN_ROOT"); d=$(q "$dir"); test ! -e \"\$d\" || { echo 'job exists; pass --reuse to reuse' >&2; exit 17; }; t=\"\${d}.tmp.\$\$\"; mkdir -p \"\$t\"/{src,reports,logs,out}; mv \"\$t\" \"\$d\""
    fi
    "${transport[@]}" "$(wrap_bash "$init_script")"
    ;;

  sync)
    [[ $# -ge 2 && $# -le 3 ]] || { usage >&2; exit 1; }
    dir="$(job_path "$1")"; local_path="$2"; remote_sub="${3:-src}"
    valid_subdir "$remote_sub" || { echo "Invalid remote subdirectory: $remote_sub" >&2; exit 1; }
    [[ -e "$local_path" ]] || { echo "Missing local path: $local_path" >&2; exit 1; }
    parent="$(cd "$(dirname "$local_path")" && pwd)"; name="$(basename "$local_path")"
    target_dir="$dir/$remote_sub/$name"
    sync_script="set -e; d=$(q "$dir"); target=$(q "$target_dir"); test -d \"\$d\"; tmp=\$(mktemp -d \"\$d/.sync.XXXXXX\"); backup=\"\${target}.old.\$\$\"; trap 'test ! -e \"\$target\" && test -e \"\$backup\" && mv \"\$backup\" \"\$target\" || true; rm -rf \"\$tmp\"' EXIT; tar -xzf - -C \"\$tmp\"; test -e \"\$tmp/$(q "$name")\"; mkdir -p $(q "$dir/$remote_sub"); test ! -e \"\$target\" || mv \"\$target\" \"\$backup\"; mv \"\$tmp/$(q "$name")\" \"\$target\"; rm -rf \"\$backup\" \"\$tmp\"; trap - EXIT"
    COPYFILE_DISABLE=1 tar -czf - -C "$parent" "$name" | "${transport[@]}" "$(wrap_bash "$sync_script")"
    ;;

  run)
    [[ $# -ge 3 ]] || { usage >&2; exit 1; }
    name="$1"; dir="$(job_path "$name")"; tool="$2"; shift 2
    valid_component "tool" "$tool"
    if [[ "$1" == --probe ]]; then
      [[ $# -eq 1 ]] || { usage >&2; exit 1; }
      exec "$EXEC_SH" "${cfg_args[@]+"${cfg_args[@]}"}" "$tool" --probe
    fi
    tmp="$(mktemp)"; trap 'rm -f "$tmp"' EXIT
    case "$1" in
      --cmd)      [[ $# -eq 2 ]] || { usage >&2; exit 1; }; printf '%s\n' "$2" >"$tmp" ;;
      --cmd-file) [[ $# -eq 2 && -f "$2" ]] || { usage >&2; exit 1; }; cp "$2" "$tmp" ;;
      --exec)
        shift; [[ $# -gt 0 ]] || { usage >&2; exit 1; }
        printf '"$EDA_TOOL_BIN"' >"$tmp"
        for arg in "$@"; do printf ' %q' "$arg" >>"$tmp"; done
        printf '\n' >>"$tmp"
        ;;
      *) usage >&2; exit 1 ;;
    esac
    encoded="$(base64 <"$tmp" | tr -d '\n')"
    run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM:-0}"
    wrapper="$(mktemp)"; trap 'rm -f "$tmp" "$wrapper"' EXIT
    cat >"$wrapper" <<EOF
set -uo pipefail
job_dir=$(q "$dir")
run_dir="\$job_dir/logs/$(q "$run_id")"
mkdir -p "\$run_dir" || exit 18
printf '%s' $(q "$encoded") | base64 -d >"\$run_dir/command.sh"
chmod 700 "\$run_dir/command.sh"
set +e
( export EDA_JOB_DIR="\$job_dir"; cd "\$job_dir"; bash "\$run_dir/command.sh" ) 2>&1 | tee "\$run_dir/run.log"
pipe_rc=("\${PIPESTATUS[@]}")
rc=\${pipe_rc[0]}
[[ \${pipe_rc[1]} -eq 0 || \$rc -ne 0 ]] || rc=74
set -e
printf '%s\n' "\$rc" >"\$run_dir/exit.code"
ln -sfn $(q "$run_id") "\$job_dir/logs/latest"
exit "\$rc"
EOF
    "$EXEC_SH" "${cfg_args[@]+"${cfg_args[@]}"}" "$tool" --cmd-file "$wrapper"
    ;;

  fetch)
    [[ $# -ge 2 && $# -le 3 ]] || { usage >&2; exit 1; }
    dir="$(job_path "$1")"; local_dir="$2"; remote_sub="${3:-reports}"
    valid_subdir "$remote_sub" || { echo "Invalid remote subdirectory: $remote_sub" >&2; exit 1; }
    mkdir -p "$local_dir"
    "${transport[@]}" "$(wrap_bash "cd $(q "$dir/$remote_sub") && tar -czf - .")" | tar -xzf - -C "$local_dir"
    ;;
esac
