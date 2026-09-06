#!/usr/bin/env bash

set -euo pipefail

script_path="$(realpath "${BASH_SOURCE[0]}")"
script_dir="$(dirname "${script_path}")"

exec node "${script_dir}/prepare_portable_python_runtime.js" "$@"
