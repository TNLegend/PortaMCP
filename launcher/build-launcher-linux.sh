#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v gcc >/dev/null 2>&1; then
  echo "gcc is required to build the Linux PortaMCP launcher." >&2
  echo "Ubuntu/Debian: sudo apt install build-essential" >&2
  exit 1
fi

gcc -std=c11 -O2 -Wall -Wextra -Werror \
  launcher/PortaMCPLauncherLinux.c \
  -o PortaMCP
chmod 755 PortaMCP
echo "Built PortaMCP"
