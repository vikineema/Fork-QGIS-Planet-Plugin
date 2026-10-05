#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "Setting up QGIS env variables ..."

# Locate QGIS binary
if ! qgis_bin=$(command -v qgis); then
    echo "Error: QGIS binary not found!"
    exit 1
fi
qgis_bin=$(readlink -f "$qgis_bin")
export QGIS_EXECUTABLE=$qgis_bin

# Extract the Nix store path (removing /bin/qgis)
qgis_prefix=$(dirname "$(dirname "$QGIS_EXECUTABLE")")
export QGIS_PREFIX_PATH=$qgis_prefix

# Construct the correct QGIS Python path
qgis_python_path="$QGIS_PREFIX_PATH/share/qgis/python"

# Check if the Python directory exists
if [[ ! -d "$qgis_python_path" ]]; then
    echo "Error: QGIS Python path not found at $qgis_python_path"
    exit 1
fi
export QGIS_PYTHON_PATH="$qgis_python_path"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$QGIS_PYTHON_PATH${QTPOSITIONING:+:$QTPOSITIONING}"

qgis_version=$(qgis --version 2>/dev/null | awk '{print $2}' | cut -d- -f1)
qgis_version_major=${qgis_version%%.*}

if [ "$qgis_version_major" -ge 4 ]; then
    export PYQT6_PATH="$QGIS_PREFIX_PATH/share/qgis/python/PyQt"
fi

#export QT_QPA_PLATFORM=offscreen

echo "✅ QGIS env variables setup complete."
echo ""

set +euo pipefail
