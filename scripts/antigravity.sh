#!/usr/bin/env bash
# Based on the vscode.sh script

# ----------------------------------------------
# User-adjustable parameters
# ----------------------------------------------

ANTIGRAVITY_USER_DIR=".antigravity"
ANTIGRAVITY_PROFILE="planet-plugin"
ANTIGRAVITY_EXT_DIR=".antigravity-extensions"
LOG_FILE="antigravity.log"
ENV_FILE=".env"

REQUIRED_EXTENSIONS=(
  # Python
  "ms-python.debugpy@2026.6.0"
  "ms-python.python@2026.4.0"
  "ms-python.vscode-python-envs@1.20.1"
  # XML
  "redhat.vscode-xml@0.29.2"
  # Makefile
  "ms-vscode.makefile-tools@0.12.17"
)

# ----------------------------------------------
# Functions
# ----------------------------------------------

launch_antigravity() {
  antigravity --user-data-dir="$ANTIGRAVITY_USER_DIR" \
    --profile="${ANTIGRAVITY_PROFILE}" \
    --extensions-dir="$ANTIGRAVITY_EXT_DIR" "$@"
}

list_installed_extensions() {
  if [[ ! -d "$ANTIGRAVITY_EXT_DIR" ]]; then
    return
  fi

  fd --hidden --no-ignore --max-depth 1 --min-depth 1 --type d --search-path "$ANTIGRAVITY_EXT_DIR" | while read -r dir; do
    pkg="$dir/package.json"
    if [[ -f "$pkg" ]]; then
      name=$(jq -r '.name' <"$pkg")
      publisher=$(jq -r '.publisher' <"$pkg")
      version=$(jq -r '.version' <"$pkg")
      echo "${publisher}.${name}@${version}"
    fi
  done
}

install_required_extensions() {
  echo "🗨️ Checking and installing required extensions..."
  local installed_exts
  installed_exts=$(list_installed_extensions)

  for ext in "${REQUIRED_EXTENSIONS[@]}"; do
      if echo "$installed_exts" | grep -q "^${ext}$"; then
          echo "  ✅ Extension ${ext} already installed."
      else
          echo "  📦 Installing ${ext}..."
          if launch_antigravity --install-extension "${ext}" >>"$LOG_FILE" 2>&1; then
              installed_exts=$(list_installed_extensions)
              if echo "$installed_exts" | grep -q "^${ext}$"; then
                  echo "  ✅ Successfully installed ${ext}."
              else
                  echo "  ❌ Failed to install ${ext} (not found after install)."
                  exit 1
              fi
          else
              echo "  ❌ Failed to install ${ext} (error during install). Check $LOG_FILE for details."
              exit 1
          fi
      fi
  done
}

clean() {
  rm -rf "$ANTIGRAVITY_USER_DIR" "$ANTIGRAVITY_EXT_DIR"
}

print_help() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

This script sets up and launches Antigravity with a custom profile and extensions for the Planet Plugin project.

Actions performed:
    - Checks for required files and directories
    - Ensures Antigravity is installed
    - Initializes Antigravity user and extension directories if needed
    - Installs all required Antigravity extensions
    - Launches Antigravity with the specified profile and directories

Options:
    --help                             Show this help message and exit
    --verbose                          Print final settings.json contents before launching Antigravity
    --list-extensions                  List installed Antigravity extensions
    --install-required-extensions      Install all required Antigravity extensions
    --clean                            Remove the "$ANTIGRAVITY_USER_DIR" and "$ANTIGRAVITY_EXT_DIR" directories completely

EOF
}

# Parameter handler
for arg in "$@"; do
  case "$arg" in
    --help)
      print_help
      exit 0
      ;;
    --verbose)
      # Handled later in the script if needed
      ;;
    --install-required-extensions)
      install_required_extensions
      exit 0
      ;;
    --list-extensions)
      echo "Installed extensions:"
      list_installed_extensions
      exit 0
      ;;
    --clean)
      echo "Removing custom Antigravity environments..."
      clean
      exit 0
      ;;
    *) ;;
  esac
done

# ----------------------------------------------
# Script starts here
# ----------------------------------------------

# Truncate the log file at the start
echo "🗨️ Truncating $LOG_FILE..."
true >"$LOG_FILE"

# Locate QGIS binary
QGIS_BIN=$(which qgis)

if [[ -z "$QGIS_BIN" ]]; then
    echo "Error: QGIS binary not found!"
    exit 1
fi

# Extract the Nix store path (removing /bin/qgis)
QGIS_PREFIX=$(dirname "$(dirname "$QGIS_BIN")")

# Construct the correct QGIS Python path
QGIS_PYTHON_PATH="$QGIS_PREFIX/share/qgis/python"

# Check if the Python directory exists
if [[ ! -d "$QGIS_PYTHON_PATH" ]]; then
    echo "Error: QGIS Python path not found at $QGIS_PYTHON_PATH"
    exit 1
fi

echo "Creating Antigravity .env file..."
cat <<EOF >"$ENV_FILE"
PYTHONPATH=$QGIS_PYTHON_PATH
QGIS_EXECUTABLE=$QGIS_BIN
QGIS_PREFIX_PATH=$QGIS_PREFIX
PYQT6_PATH=$QGIS_PREFIX/share/qgis/python/PyQt
QT_QPA_PLATFORM=offscreen
EOF

echo "✅ .env file created successfully!"
echo "Contents of .env:"
cat "$ENV_FILE"

# Also set the python path in this shell in case we want to run tests etc from the command line
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$QGIS_PYTHON_PATH"

echo "🗨️ Checking Antigravity is installed ..."
if ! command -v antigravity &>/dev/null; then
    echo "  ❌ 'antigravity' CLI not found. Please install Antigravity and add 'antigravity' to your PATH."
    exit 1
else
    echo "  ✅ Antigravity found ok."
fi

echo "🗨️  Checking if Antigravity has been run before..."
if [[ ! -d "$ANTIGRAVITY_USER_DIR" ]]; then
    echo "  🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻🔻"
    echo "  ⭐️ It appears you have not run antigravity in this project before."
    echo "     After it opens, please close antigravity and then rerun this script"
    echo "     so that the extensions directory initialises properly."
    echo "  🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺🔺"
    mkdir -p "$ANTIGRAVITY_USER_DIR"
    mkdir -p "$ANTIGRAVITY_EXT_DIR"
    # Launch Antigravity with the sandboxed environment
    launch_antigravity .
    exit 1
else
    echo "  ✅ Antigravity directory found from previous runs of antigravity."
fi

# Automatically sync extensions on script run before boot
install_required_extensions

echo "🗨️ Launching Antigravity..."
launch_antigravity .
