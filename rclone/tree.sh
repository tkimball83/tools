#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

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

function usage() {
  cat <<EOF
Usage: $(basename -- "${0}") [options]

Options:
  -2          Enable HTTP/2 for rclone
  -b <path>   Path to rclone binary (default: rclone)
  -c <path>   Path to rclone config (default: first found config)
  -d          Enable rclone dry run
  -f <path>   Path to tree YAML (default: ${SCRIPT_DIR}/tree.yaml)
  -h          Show this help
  -k <count>  rclone checkers (default: 32)
  -l <limit>  rclone bandwidth limit (default: 0)
  -q          Disable rclone verbose output
  -s <path>   Path to shyaml binary (default: shyaml)
  -t <count>  rclone transfers (default: 16)
  -v          rclone verbose level (default: -v, repeat for debug, e.g. -vv)
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

  output_command "${command[@]}"
  echo

  "${command[@]}"
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

RCLONE_BIN=rclone
RCLONE_BWLIMIT=0
RCLONE_CHECKERS=32
RCLONE_CONFIG=
RCLONE_DRY_RUN=false
RCLONE_ENABLE_HTTP2=false
RCLONE_TRANSFERS=16
RCLONE_QUIET=false
RCLONE_VERBOSE=
SHYAML_BIN=shyaml
TREE_YAML="${SCRIPT_DIR}/tree.yaml"

# These names are also rclone environment variables. Settings are passed as
# flags, so keep them out of rclone's environment to avoid conflicting values.
export -n RCLONE_BWLIMIT \
  RCLONE_CHECKERS \
  RCLONE_CONFIG \
  RCLONE_DRY_RUN \
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
    q) RCLONE_QUIET=true ;;
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

if [[ "${RCLONE_QUIET}" == true ]]; then
  [[ -z "${RCLONE_VERBOSE}" ]] || fail "Options -q and -v cannot be combined."
else
  RCLONE_VERBOSE=${RCLONE_VERBOSE:-v}
fi

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

[[ "$(yaml_type tree)" == sequence ]] || fail "Unable to parse a tree list from ${TREE_YAML}."

tree_length=$(yaml_length tree)
((tree_length > 0)) || fail "The rclone tree list is empty."

trap 'fail "Interrupted."' INT TERM

failures=0
syncs=0

for ((t = 0; t < tree_length; t++)); do
  trunk=$(yaml_value "tree.${t}.trunk")
  branch_length=$(yaml_length "tree.${t}.branches")

  if [[ -z "${trunk}" ]]; then
    warn "The rclone trunk is not defined, skipping."
    continue
  fi

  if ((branch_length == 0)); then
    warn "The rclone tree for ${trunk} has no branches, skipping."
    continue
  fi

  for ((b = 0; b < branch_length; b++)); do
    branch=$(yaml_value "tree.${t}.branches.${b}.name")
    leafs_key="tree.${t}.branches.${b}.leafs"
    leafs_type=$(yaml_type "${leafs_key}")

    if [[ -z "${branch}" ]]; then
      warn "The rclone branch has no name in ${trunk}, skipping."
      continue
    fi

    if [[ "${branch}" == "${trunk}" ]]; then
      warn "The rclone branch ${branch} is the same as its trunk, skipping."
      continue
    fi

    # Only a missing leafs key syncs the whole remote; a malformed one is skipped.
    if [[ -z "${leafs_type}" ]]; then
      syncs=$((syncs + 1))
      rclone_sync "${trunk}:" "${branch}:" || failures=$((failures + 1))
      continue
    fi

    if [[ "${leafs_type}" != sequence ]] || (($(yaml_length "${leafs_key}") == 0)); then
      warn "The rclone leafs for ${trunk} -> ${branch} must be a non-empty list, skipping."
      continue
    fi

    while IFS= read -r -d '' leaf; do
      if [[ -z "${leaf}" ]]; then
        warn "The rclone leaf is not defined in ${trunk} -> ${branch}, skipping."
        continue
      fi

      syncs=$((syncs + 1))
      rclone_sync "${trunk}:${leaf}" "${branch}:${leaf}" </dev/null || failures=$((failures + 1))
    done < <(yaml get-values-0 "${leafs_key}")
  done
done

((failures == 0)) || fail "${failures} of ${syncs} rclone sync command(s) failed."

output "Completed ${syncs} rclone sync command(s)."
