#!/bin/sh
# Move the server's virtual environment to another Python version, safely.
#
#   sudo sh deploy/upgrade-python.sh            # Python 3.15 (default)
#   sudo sh deploy/upgrade-python.sh 3.15
#   sudo sh deploy/upgrade-python.sh --rollback # put the previous environment back
#
# What it does: installs that Python with uv (for the agent user, nothing system-wide), builds a NEW environment at
# .venv-next, installs requirements.txt into it, and runs the whole offline test suite with it. Only if every test
# passes does it stop the services, keep the current environment as .venv-prev, move .venv-next into place and start
# the services again. If anything fails before that point, the running environment is untouched. The .env file and
# state/ are never read or changed.
set -eu
APP=/opt/trading-agent
USER_NAME=agent
cd "$APP"

restart() { systemctl restart trading-agent-watch trading-agent-dashboard && systemctl is-active trading-agent-watch trading-agent-dashboard; }

if [ "${1:-}" = "--rollback" ]; then
  [ -d .venv-prev ] || { echo "No .venv-prev to roll back to."; exit 1; }
  systemctl stop trading-agent-watch trading-agent-dashboard
  rm -rf .venv-failed && mv .venv .venv-failed && mv .venv-prev .venv
  restart
  echo "Rolled back to: $(.venv/bin/python --version)"
  exit 0
fi

VERSION="${1:-3.15}"
UV="/home/$USER_NAME/.local/bin/uv"
if [ ! -x "$UV" ]; then
  echo "Installing uv for $USER_NAME..."
  sudo -u "$USER_NAME" -H sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi
sudo -u "$USER_NAME" -H "$UV" self update >/dev/null 2>&1 || true
sudo -u "$USER_NAME" -H "$UV" python install "$VERSION"
rm -rf .venv-next
sudo -u "$USER_NAME" -H "$UV" venv --seed -p "$VERSION" .venv-next
sudo -u "$USER_NAME" -H "$UV" pip install -q -p .venv-next/bin/python -r requirements.txt
sudo -u "$USER_NAME" -H .venv-next/bin/python -m pip --version >/dev/null   # the update command needs python -m pip
echo "Testing with $(.venv-next/bin/python --version) (offline; takes a few minutes)..."
if ! sudo -u "$USER_NAME" -H .venv-next/bin/python -m pytest -q -p no:cacheprovider; then
  echo "Tests failed on Python $VERSION: nothing was switched. The services still run $(.venv/bin/python --version)."
  exit 1
fi
systemctl stop trading-agent-watch trading-agent-dashboard
rm -rf .venv-prev && mv .venv .venv-prev && mv .venv-next .venv
restart
echo "Now running $(.venv/bin/python --version). The old environment is kept in .venv-prev;"
echo "undo with: sudo sh deploy/upgrade-python.sh --rollback"
