#!/bin/sh
# Build and verify Linux packages locally using an isolated Ubuntu 22.04 container.
# Run with the repository mounted read-only at /source and release output at /out.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends python3 python3-venv libpython3.10 binutils ca-certificates >/tmp/cometapi-apt.log
python3 -m venv /tmp/cometapi-build-runtime
/tmp/cometapi-build-runtime/bin/python -m pip install --disable-pip-version-check --no-cache-dir -q -r /source/requirements.txt PyInstaller==6.22.2
/tmp/cometapi-build-runtime/bin/python /source/scripts/build_release.py --output /out
