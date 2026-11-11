#!/usr/bin/env bash
set -uo pipefail

# The script's real directory, following symlinks (e.g. from ~/bin) and
# ignoring CDPATH, which would otherwise make cd print the directory.
function script_dir() {
  local source="${BASH_SOURCE[0]}"
  local dir

  while [[ -L "${source}" ]]; do
    dir=$(CDPATH='' cd -P -- "$(dirname -- "${source}")" && pwd)
    source=$(readlink -- "${source}")
    [[ "${source}" == /* ]] || source="${dir}/${source}"
  done

  CDPATH='' cd -P -- "$(dirname -- "${source}")" && pwd
}

SCRIPT_DIR=$(script_dir)

declare -a RCLONE_CONFIG_PATHS=(
  "${HOME}/.config/rclone/rclone.conf"
  "/etc/rclone/rclone.conf"
)

function output() {
  printf '[*]: %s\n' "${1}"
}

function warn() {
  output "WARNING: ${1}" >&2
}

function fail() {
  output "ERROR: ${1}" >&2
  exit 1
}

function interrupted() {
  local -a pids=()

  # Stop the running sync too, rather than leaving it to finish on its own.
  # rclone is the only background job, and jobs -p is right at any moment,
  # unlike $!, which bash 3.2 cannot even read under set -u before a job.
  read -r -d '' -a pids <<<"$(jobs -p)"
  if ((${#pids[@]} > 0)); then
    kill -TERM "${pids[@]}" 2>/dev/null
    wait "${pids[@]}" 2>/dev/null
  fi

  echo >&2
  output "ERROR: Interrupted." >&2
  exit "${1}"
}

# Print the most useful line of a tool's error output.
function error_summary() {
  local summary

  summary=$(grep -E '^[[:alnum:]_.]*(Error|error): ' <<<"${1}" | tail -n 1)
  [[ -n "${summary}" ]] || summary=$(tail -n 1 <<<"${1}")

  printf '%s\n' "${summary}"
}

function usage() {
  cat <<EOF
Usage: $(basename -- "${0}") [options]

Options:
  -2          Enable HTTP/2 for rclone
  -b <path>   Path to rclone binary (default: rclone)
  -c <path>   Path to rclone config (default: \$RCLONE_CONFIG or first found)
  -d          Enable rclone dry run
  -f <path>   Path to tree YAML (default: ${SCRIPT_DIR}/tree.yaml)
  -h          Show this help
  -k <count>  rclone checkers (default: 32)
  -l <limit>  rclone bandwidth limit (default: 0)
  -q          Disable rclone progress output (default: on when interactive)
  -s <path>   Path to shyaml binary (default: shyaml)
  -t <count>  rclone transfers (default: 16)
  -v          Enable rclone verbose output (repeat for more, e.g. -vv)
EOF
}

function output_command() {
  local argument

  output "${1} ${2}"

  for argument in "${@:3}"; do
    output "  ${argument}"
  done
}

function rclone_sync() {
  local source="${1}"
  local destination="${2}"
  local -a command=("${RCLONE_BIN}" sync "${RCLONE_ARGS[@]}" "${source}" "${destination}")
  local status=0

  output_command "${command[@]}"
  echo

  # Run rclone in the background and wait for it, so the INT/TERM traps run
  # straight away instead of after the sync. Output still goes to the
  # terminal as usual, and <&0 keeps stdin for prompts.
  "${command[@]}" <&0 &
  wait "${!}" || status=$?
  echo

  return "${status}"
}

function require_command() {
  local command_path="${1}"

  if [[ "${command_path}" == */* ]]; then
    [[ -x "${command_path}" ]] || fail "Unable to execute ${command_path}."
  else
    command -v "${command_path}" >/dev/null 2>&1 || fail "Unable to locate ${command_path}."
  fi
}

function require_file() {
  [[ -f "${1}" ]] || fail "Unable to locate ${1}."
}

function require_positive_integer() {
  local name="${1}"
  local value="${2}"

  [[ "${value}" =~ ^[1-9][0-9]*$ ]] || fail "The ${name} value must be a positive integer: ${value}"
}

function resolve_rclone_config() {
  local config_path

  for config_path in "${RCLONE_CONFIG_PATHS[@]}"; do
    if [[ -f "${config_path}" ]]; then
      printf '%s\n' "${config_path}"
      return 0
    fi
  done

  return 1
}

function yaml() {
  "${SHYAML_BIN}" "$@" <<<"${TREE_DATA}" 2>/dev/null
}

function yaml_length() {
  local length

  length=$(yaml get-length "${1}")

  [[ "${length}" =~ ^[0-9]+$ ]] || length=0

  printf '%s\n' "${length}"
}

function yaml_type() {
  yaml get-type "${1}"
}

function yaml_value() {
  yaml get-value "${1}"
}

# Print the non-empty string at a key, or fail. This rejects values YAML turns
# into other types, such as null, yes, 1.10 or dates, and any control
# character. Those are counted in shyaml's raw output, because the shell would
# change the value: $(...) drops trailing newlines and NUL bytes ("Docs\n" and
# "Doc\0s" would both become "Docs"), and a newline would split it into lines.
function yaml_string() {
  local controls
  local value

  [[ "$(yaml_type "${1}")" == str ]] || return 1
  controls=$(yaml_value "${1}" | LC_ALL=C tr -dc '\000-\037\177' | wc -c) || return 1
  ((controls == 0)) || return 1
  value=$(yaml_value "${1}") || return 1
  [[ -n "${value}" ]] || return 1

  printf '%s\n' "${value}"
}

# Print the keys of the mapping at $1, one per line, or fail. Failing closed
# matters: if shyaml itself failed, a key would look missing, and a missing
# leafs key means syncing the whole remote.
function yaml_keys() {
  local keys

  [[ "$(yaml_type "${1}")" == struct ]] || return 1
  keys=$(yaml keys-0 "${1}" | tr '\0' '\n') || return 1

  printf '%s\n' "${keys}"
}

# Print the keys in $1 (one per line) that are not listed in the other arguments.
function unknown_keys() {
  local allowed
  local key
  local unknown=""

  while IFS= read -r key; do
    [[ -n "${key}" ]] || continue
    for allowed in "${@:2}"; do
      [[ "${key}" == "${allowed}" ]] && continue 2
    done
    unknown+="${unknown:+, }${key}"
  done <<<"${1}"

  printf '%s\n' "${unknown}"
}

# Print the remote name at a key, without a trailing colon, or fail unless it
# is a remote in the rclone config. Anything else would fail mid-run, or be
# taken by rclone as a local path (e.g. "/Volumes/Backup:").
function yaml_remote() {
  local remote

  remote=$(yaml_string "${1}") || return 1
  remote=${remote%:}
  [[ -n "${remote}" ]] && grep -qxF -- "${remote}:" <<<"${REMOTES}" || return 1

  printf '%s\n' "${remote}"
}

# True for a leaf with a single spelling: relative, with no ".", ".." or empty
# parts, so overlapping paths are caught (rclone reads "x:." as "x:", and
# "x:./Docs" or "x:/Docs" as "x:Docs").
function valid_leaf() {
  case "/${1}/" in
    //* | *//* | */./* | */../*) return 1 ;;
  esac
}

# True if one rclone path is the same as, or inside, the other.
function paths_overlap() {
  local a="${1}"
  local b="${2}"

  [[ "${a}" == "${b}" ]] && return 0
  [[ "${a}" == *: && "${b}" == "${a}"* ]] && return 0
  [[ "${b}" == *: && "${a}" == "${b}"* ]] && return 0
  [[ "${b}" == "${a}/"* || "${a}" == "${b}/"* ]]
}

function config_error() {
  output "ERROR: ${TREE_YAML}: ${1}" >&2
  config_errors=$((config_errors + 1))
}

# Queue a sync, skipping exact duplicates.
function plan_sync() {
  local i

  for ((i = 0; i < ${#SOURCES[@]}; i++)); do
    if [[ "${SOURCES[i]}" == "${1}" && "${DESTINATIONS[i]}" == "${2}" ]]; then
      warn "Skipping duplicate sync ${1} -> ${2}."
      return 0
    fi
  done

  SOURCES+=("${1}")
  DESTINATIONS+=("${2}")
}

RCLONE_BIN=rclone
RCLONE_BWLIMIT=0
RCLONE_CHECKERS=32
RCLONE_CONFIG=${RCLONE_CONFIG-}
RCLONE_DRY_RUN=false
RCLONE_ENABLE_HTTP2=false
RCLONE_PROGRESS=true
RCLONE_TRANSFERS=16
RCLONE_VERBOSE=
SHYAML_BIN=shyaml
TREE_YAML="${SCRIPT_DIR}/tree.yaml"

# These names are also rclone environment variables. Settings are passed as
# flags, so keep them out of rclone's environment to avoid conflicting values.
export -n RCLONE_BWLIMIT \
  RCLONE_CHECKERS \
  RCLONE_CONFIG \
  RCLONE_DRY_RUN \
  RCLONE_PROGRESS \
  RCLONE_TRANSFERS \
  RCLONE_VERBOSE

while getopts ":2b:c:df:hk:l:qs:t:v" opt; do
  case "${opt}" in
    2) RCLONE_ENABLE_HTTP2=true ;;
    b) RCLONE_BIN=${OPTARG} ;;
    c) RCLONE_CONFIG=${OPTARG} ;;
    d) RCLONE_DRY_RUN=true ;;
    f) TREE_YAML=${OPTARG} ;;
    h)
      usage
      exit 0
      ;;
    k) RCLONE_CHECKERS=${OPTARG} ;;
    l) RCLONE_BWLIMIT=${OPTARG} ;;
    q) RCLONE_PROGRESS=false ;;
    s) SHYAML_BIN=${OPTARG} ;;
    t) RCLONE_TRANSFERS=${OPTARG} ;;
    v) RCLONE_VERBOSE+=v ;;
    :)
      usage >&2
      fail "Option -${OPTARG} requires an argument."
      ;;
    *)
      usage >&2
      fail "Invalid option: -${OPTARG}"
      ;;
  esac
done
shift $((OPTIND - 1))

(($# == 0)) || {
  usage >&2
  fail "Unexpected argument(s): $*"
}

if [[ -z "${RCLONE_CONFIG}" ]]; then
  RCLONE_CONFIG=$(resolve_rclone_config) || fail "Unable to locate rclone config."
fi

require_command "${RCLONE_BIN}"
require_command "${SHYAML_BIN}"
require_file "${RCLONE_CONFIG}"
require_file "${TREE_YAML}"
require_positive_integer checkers "${RCLONE_CHECKERS}"
require_positive_integer transfers "${RCLONE_TRANSFERS}"

# Read the tree once so edits made during a long run do not change it midway.
TREE_DATA=$(<"${TREE_YAML}") || fail "Unable to read ${TREE_YAML}."

declare -a RCLONE_ARGS=(
  "--bwlimit=${RCLONE_BWLIMIT}"
  "--checkers=${RCLONE_CHECKERS}"
  "--config=${RCLONE_CONFIG}"
  "--transfers=${RCLONE_TRANSFERS}"
)

[[ "${RCLONE_DRY_RUN}" == true ]] && RCLONE_ARGS+=(--dry-run)
[[ "${RCLONE_ENABLE_HTTP2}" == false ]] && RCLONE_ARGS+=(--disable-http2)
[[ -n "${RCLONE_VERBOSE}" ]] && RCLONE_ARGS+=("-${RCLONE_VERBOSE}")
# Progress redraws the terminal, so only show it when stdout is one.
[[ "${RCLONE_PROGRESS}" == true && -t 1 ]] && RCLONE_ARGS+=(--progress)

# Let rclone check the binary and every option once, before any sync starts.
if ! preflight=$("${RCLONE_BIN}" "${RCLONE_ARGS[@]}" version 2>&1); then
  fail "rclone rejected the options: $(error_summary "${preflight}")"
fi

# Remote names are checked against this list. stderr is left alone so rclone
# can show errors, or ask for the password of an encrypted config.
REMOTES=$("${RCLONE_BIN}" --config="${RCLONE_CONFIG}" listremotes) ||
  fail "Unable to list the remotes in ${RCLONE_CONFIG}."

# Parse once with errors visible; later lookups hide expected missing-key errors.
if ! tree_type=$("${SHYAML_BIN}" get-type tree <<<"${TREE_DATA}" 2>&1); then
  fail "Unable to parse ${TREE_YAML}: $(error_summary "${tree_type}")"
fi
[[ "${tree_type}" == sequence ]] || fail "Unable to parse a tree list from ${TREE_YAML}."

tree_length=$(yaml_length tree)
((tree_length > 0)) || fail "The rclone tree list is empty."

# Check the whole tree and build the list of syncs before running any, so a
# mistake in the file (a misspelled "leafs" would otherwise sync the whole
# remote) stops the run before anything is synced.
config_errors=0
declare -a SOURCES=()
declare -a DESTINATIONS=()

for ((t = 0; t < tree_length; t++)); do
  entry="tree.${t}"
  if ! keys=$(yaml_keys "${entry}"); then
    config_error "${entry} must be a mapping with trunk and branches."
    continue
  fi
  unknown=$(unknown_keys "${keys}" trunk branches)
  [[ -z "${unknown}" ]] || config_error "${entry} has unknown key(s): ${unknown}"

  if ! trunk=$(yaml_remote "${entry}.trunk"); then
    config_error "${entry}.trunk must be a remote from ${RCLONE_CONFIG}."
    continue
  fi

  branch_length=0
  if [[ "$(yaml_type "${entry}.branches")" == sequence ]]; then
    branch_length=$(yaml_length "${entry}.branches")
  fi
  if ((branch_length == 0)); then
    config_error "${entry}.branches for ${trunk} must be a non-empty list."
    continue
  fi

  for ((b = 0; b < branch_length; b++)); do
    entry="tree.${t}.branches.${b}"
    if ! keys=$(yaml_keys "${entry}"); then
      config_error "${entry} must be a mapping with a name."
      continue
    fi
    unknown=$(unknown_keys "${keys}" name leafs)
    if [[ -n "${unknown}" ]]; then
      config_error "${entry} has unknown key(s): ${unknown}"
      continue
    fi

    if ! branch=$(yaml_remote "${entry}.name"); then
      config_error "${entry}.name must be a remote from ${RCLONE_CONFIG}."
      continue
    fi

    if [[ "${branch}" == "${trunk}" ]]; then
      config_error "${entry} syncs ${trunk} to itself."
      continue
    fi

    # Without a leafs key, the branch gets the whole trunk remote. Whether
    # it has one comes from the key list, never from a failed lookup.
    if ! grep -qxF leafs <<<"${keys}"; then
      plan_sync "${trunk}:" "${branch}:"
      continue
    fi

    leafs_key="${entry}.leafs"
    leaf_length=0
    if [[ "$(yaml_type "${leafs_key}")" == sequence ]]; then
      leaf_length=$(yaml_length "${leafs_key}")
    fi
    if ((leaf_length == 0)); then
      config_error "${leafs_key} for ${trunk} -> ${branch} must be a non-empty list."
      continue
    fi
    for ((l = 0; l < leaf_length; l++)); do
      if ! leaf=$(yaml_string "${leafs_key}.${l}"); then
        config_error "${leafs_key}.${l} must be a non-empty string."
        continue
      fi
      leaf=${leaf%/}
      if ! valid_leaf "${leaf}"; then
        config_error "${leafs_key}.${l} must be a relative path without '.', '..' or empty parts: ${leaf}"
        continue
      fi
      plan_sync "${trunk}:${leaf}" "${branch}:${leaf}"
    done
  done
done

# rclone sync makes each destination match its source, deleting anything else,
# so syncs must not write to overlapping places or read from a remote that
# another sync writes to; otherwise the result depends on their order.
chained=""
for ((i = 0; i < ${#DESTINATIONS[@]}; i++)); do
  for ((j = i + 1; j < ${#DESTINATIONS[@]}; j++)); do
    if paths_overlap "${DESTINATIONS[i]}" "${DESTINATIONS[j]}"; then
      config_error "${SOURCES[i]} -> ${DESTINATIONS[i]} and ${SOURCES[j]} -> ${DESTINATIONS[j]} write to overlapping paths."
    fi
  done
  remote=${DESTINATIONS[i]%%:*}
  # Report each remote once (remote names may contain spaces, hence lines).
  grep -qxF -- "${remote}" <<<"${chained}" && continue
  for ((j = 0; j < ${#SOURCES[@]}; j++)); do
    if [[ "${remote}" == "${SOURCES[j]%%:*}" ]]; then
      config_error "${remote} is both synced to and synced from."
      chained+="${remote}"$'\n'
      break
    fi
  done
done

((config_errors == 0)) || fail "Found ${config_errors} problem(s) in ${TREE_YAML}; nothing was synced."

trap 'interrupted 130' INT
trap 'interrupted 143' TERM

failures=0
syncs=${#SOURCES[@]}

for ((i = 0; i < syncs; i++)); do
  rclone_sync "${SOURCES[i]}" "${DESTINATIONS[i]}" || failures=$((failures + 1))
done

((failures == 0)) || fail "${failures} of ${syncs} rclone sync command(s) failed."

output "Completed ${syncs} rclone sync command(s)."
