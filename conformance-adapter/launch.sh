#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
# Launcher for the Python conformance adapter. It drives the runtime that contains this
# directory (governed-objects-python/src). The conformance runner treats it exactly like a
# third-party adapter:
#
#   python -m runner --adapter-command "path/to/governed-objects-python/conformance-adapter/launch.sh"
#
# Requirements: a Python (3.11+) environment that already has the runtime's dependencies
# (pydantic, pyyaml). Set CONFORMANCE_PYTHON to that interpreter; nothing is installed.
# Optional: CONFORMANCE_IMPLEMENTATION_VERSION sets the self-declared version.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
exec "${CONFORMANCE_PYTHON:-python3}" "$here/adapter.py"
