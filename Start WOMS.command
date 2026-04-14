#!/bin/bash
# ============================================================
#  WOMS — Work Order Management System (macOS Launcher)
#  Double-click this file to start WOMS.
#  Automatically installs Python if needed.
# ============================================================

PYTHON_VERSION="3.12.7"

# Move to the folder this script lives in
cd "$(dirname "$0")"

clear
echo ""
echo "  ============================================="
echo "   WOMS  —  Work Order Management System"
echo "  ============================================="
echo ""

# ---- Ensure Python 3 is available ----
install_python() {
    echo "  Python 3 is not installed. Setting it up now..."
    echo ""

    PKG_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-macos11.pkg"
    PKG_PATH="/tmp/python-${PYTHON_VERSION}-install.pkg"

    echo "  Downloading Python ${PYTHON_VERSION}..."
    curl -fSL --progress-bar -o "$PKG_PATH" "$PKG_URL"

    if [ $? -eq 0 ] && [ -f "$PKG_PATH" ]; then
        echo ""
        echo "  -------------------------------------------------------"
        echo "  A Python installer window will now open."
        echo "  Please click through the steps to install Python."
        echo "  When it finishes, come back to this window."
        echo "  -------------------------------------------------------"
        echo ""

        # Open the macOS installer GUI (familiar wizard for non-technical users)
        open "$PKG_PATH"

        # Wait for python3 to become available (user is clicking through wizard)
        echo "  Waiting for Python installation to complete..."
        TRIES=0
        MAX_TRIES=120  # wait up to ~10 minutes
        while [ $TRIES -lt $MAX_TRIES ]; do
            if command -v python3 &>/dev/null; then
                break
            fi
            if [ -x "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" ]; then
                export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"
                break
            fi
            if [ -x "/usr/local/bin/python3" ]; then
                break
            fi
            sleep 5
            TRIES=$((TRIES + 1))
        done

        rm -f "$PKG_PATH"

        if ! command -v python3 &>/dev/null; then
            echo ""
            echo "  [!] Python installation didn't complete."
            echo "      Please install Python manually from:"
            echo "      https://www.python.org/downloads/"
            echo "      Then double-click this file again."
            echo ""
            echo "  Press any key to exit..."
            read -n 1
            exit 1
        fi

        echo "  Python installed successfully!"
        echo ""
    else
        echo ""
        echo "  Could not download Python automatically."
        echo "  Opening the Python download page in your browser..."
        echo ""
        open "https://www.python.org/downloads/"
        echo "  After installing Python, double-click this file again."
        echo ""
        echo "  Press any key to exit..."
        read -n 1
        exit 1
    fi
}

if ! command -v python3 &>/dev/null; then
    # Also check the standard framework install location (may not be on PATH yet)
    if [ -x "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" ]; then
        export PATH="/Library/Frameworks/Python.framework/Versions/3.12/bin:$PATH"
    elif [ -x "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3" ]; then
        export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:$PATH"
    else
        install_python
    fi
fi

echo "  Using $(python3 --version)"
echo ""

# ---- First-time setup or broken venv: recreate if deps fail ----
setup_venv() {
    echo "  Installing app dependencies..."
    echo "  (This only happens once and takes about a minute.)"
    echo ""
    rm -rf .venv
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "  [!] Failed to create virtual environment."
        echo "      Please make sure Python 3 is installed correctly."
        echo ""
        echo "  Press any key to exit..."
        read -n 1
        exit 1
    fi
    source .venv/bin/activate
    pip install --quiet --disable-pip-version-check -r requirements.txt
    if [ $? -ne 0 ]; then
        echo ""
        echo "  [!] Failed to install dependencies."
        echo "      Please check your internet connection and try again."
        echo ""
        echo "  Press any key to exit..."
        read -n 1
        exit 1
    fi
    echo "  Setup complete!"
    echo ""
}

if [ ! -d ".venv" ]; then
    setup_venv
else
    source .venv/bin/activate
    # Verify deps are actually working, not just installed
    python3 -c "import fastapi, uvicorn, multipart, httpx, aiosqlite, dotenv" 2>/dev/null
    if [ $? -ne 0 ]; then
        echo "  Dependencies are missing or broken. Reinstalling..."
        echo ""
        setup_venv
    fi
fi

# ---- Ensure .env exists ----
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
    else
        cat > .env <<'ENVEOF'
SC_API_TOKEN=
SC_API_BASE=https://api.safetyculture.io
SYNC_INTERVAL_SECONDS=10
ENVEOF
    fi
fi

# ---- Always prompt for API token ----
CURRENT_TOKEN=$(grep 'SC_API_TOKEN=' .env | cut -d= -f2-)

echo "  -------------------------------------------------------"
echo "  SafetyCulture API Token"
echo "  -------------------------------------------------------"
echo ""
echo "  Enter your SafetyCulture API token to sync work orders."
echo "  (Find it in SafetyCulture > Company Settings >"
echo "   Integrations > API Tokens)"
echo ""
echo "  Press Enter to run in demo mode (no live sync)."
if [ -n "$CURRENT_TOKEN" ]; then
    echo ""
    echo "  Current: ${CURRENT_TOKEN:0:8}...${CURRENT_TOKEN: -4}"
    echo "  (Press Enter to keep the current token.)"
fi
echo ""
printf "  API Token: "
read SC_TOKEN

if [ -n "$SC_TOKEN" ]; then
    sed -i '' "s|SC_API_TOKEN=.*|SC_API_TOKEN=${SC_TOKEN}|" .env
    echo ""
    echo "  Token saved!"
elif [ -z "$CURRENT_TOKEN" ]; then
    echo ""
    echo "  No token — running in demo mode."
fi
echo ""

# ---- Launch ----
python3 run.py

echo ""
echo "  Press any key to close this window..."
read -n 1
