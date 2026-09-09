#!/usr/bin/env bash
# Install the official Interactive Brokers Python API (ibapi) into ./.venv
#
# Why this script exists instead of a line in pyproject.toml:
#
#   1. PyPI's `ibapi` is 9.81.1.post1 -- a stale mirror from ~2020. IBKR's
#      current stable is 10.45. Installing from PyPI silently gets you a
#      six-version-old client.
#   2. The official distribution is a zip of the whole TWS API (Java, C++,
#      C#, Python). The Python package sits at IBJts/source/pythonclient,
#      so a direct-URL dependency cannot reach it.
#   3. The API is under the IB API Non-Commercial License. Vendoring the
#      source into this public repo would redistribute IBKR's code, so we
#      download it at install time instead.
#
# Usage:  ./scripts/install_ibapi.sh

set -euo pipefail

TWSAPI_VERSION="1045.01"
TWSAPI_SHA256="56ea048911052e86d6621ab712957c790fce6d547bc2a55900136ae4f6835941"
TWSAPI_URL="https://interactivebrokers.github.io/downloads/twsapi_macunix.${TWSAPI_VERSION}.zip"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="${REPO_ROOT}/.venv/bin/python"

if ! command -v uv >/dev/null 2>&1; then
  echo "error: uv not found (brew install uv)." >&2
  exit 1
fi
if [[ ! -x "${VENV_PY}" ]]; then
  echo "error: ${VENV_PY} not found. Run 'uv venv --python 3.12' first." >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

echo "==> Downloading TWS API ${TWSAPI_VERSION}"
curl -fsSL --max-time 300 -o "${WORKDIR}/twsapi.zip" "${TWSAPI_URL}"

echo "==> Verifying checksum"
ACTUAL="$(shasum -a 256 "${WORKDIR}/twsapi.zip" | awk '{print $1}')"
if [[ "${ACTUAL}" != "${TWSAPI_SHA256}" ]]; then
  cat >&2 <<MSG
error: checksum mismatch for twsapi_macunix.${TWSAPI_VERSION}.zip
  expected: ${TWSAPI_SHA256}
  actual:   ${ACTUAL}

IBKR sometimes republishes an archive under the same filename. Verify the
download by hand, then update TWSAPI_SHA256 in this script.
MSG
  exit 1
fi

echo "==> Extracting"
unzip -q "${WORKDIR}/twsapi.zip" 'IBJts/source/pythonclient/*' -d "${WORKDIR}"

echo "==> Installing into .venv"
# uv-created venvs do not ship pip; uv installs against the venv interpreter.
uv pip install --quiet --python "${VENV_PY}" "${WORKDIR}/IBJts/source/pythonclient"

echo "==> Verifying"
"${VENV_PY}" - <<'PY'
import ibapi
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
print(f"ibapi {ibapi.get_version_string()} installed OK")
PY
