#!/usr/bin/env bash
# Small, side-effect-free helpers shared by deployment scripts.

read_env_value() {
  local key="$1" file="$2"
  [[ -f "$file" ]] || return 0
  awk -v key="$key" '
    index($0, key "=") == 1 { value = substr($0, length(key) + 2) }
    END { print value }
  ' "$file" | tr -d '[:space:]'
}
