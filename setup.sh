#!/usr/bin/env bash
# setup.sh
#
# One-shot local setup for raghu_assign.
# Run ONCE before the first ./install.
#
# What it does:
#   1. Creates venv and installs deps from PyPI (bypasses corporate pip mirror)
#   2. Generates ~/.ssh/nso_id (ed25519) if it doesn't exist yet
#   3. Ensures clouds.yaml is present (prompts for password if REPLACE_ME is
#      still in the file)
#
# After this script you only need to run:
#   ./check_cloud.py ./clouds.yaml    (verify auth & cloud params)
#   ./install ./clouds.yaml <tag> ~/.ssh/nso_id

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

echo ""
echo "==> Step 1/3: Python virtual environment"
if [[ -d venv ]]; then
    echo "    venv already exists, skipping."
else
    python3 -m venv venv
    echo "    Created venv."
fi

echo ""
echo "==> Step 2/3: Installing Python dependencies (from public PyPI)"
./venv/bin/pip --no-cache-dir install \
    --index-url https://pypi.org/simple \
    --quiet \
    openstacksdk>=2.1.0 \
    "ansible>=8.0.0" \
    "ansible-core>=2.15.0" \
    "requests>=2.31.0" \
    "pyyaml>=6.0" \
    "jinja2>=3.1.2" \
    "flask>=2.3.0" \
    "ping3>=4.0.4"
echo "    All dependencies installed."

echo ""
echo "==> Step 3/3: SSH key"
if [[ -f ~/.ssh/nso_id ]]; then
    echo "    ~/.ssh/nso_id already exists, skipping."
else
    ssh-keygen -t ed25519 -f ~/.ssh/nso_id -N '' -C 'nso-project'
    echo "    Generated ~/.ssh/nso_id (no passphrase)."
fi

echo ""
echo "==> clouds.yaml check"
if [[ ! -f "$HERE/clouds.yaml" ]]; then
    echo "    ERROR: clouds.yaml not found in $HERE"
    echo "    Copy your OpenStack clouds.yaml here and re-run setup.sh."
    exit 1
fi

if grep -q 'REPLACE_ME' "$HERE/clouds.yaml"; then
    echo ""
    echo "    The clouds.yaml still contains 'REPLACE_ME' as the password."
    echo "    Enter your Horizon password now (it will be written to clouds.yaml):"
    read -r -s -p "    Password: " PASS
    echo ""
    # Use Python to do the replacement so we don't mangle YAML quoting
    ./venv/bin/python3 - <<PYEOF
import re, pathlib
p = pathlib.Path("clouds.yaml")
txt = p.read_text()
txt = re.sub(r'(password:\s*["\']?)REPLACE_ME(["\']?)', r'\g<1>${PASS}\g<2>', txt)
p.write_text(txt)
PYEOF
    # The heredoc above doesn't expand $PASS inside python; do it with sed:
    sed -i '' "s/REPLACE_ME/$PASS/" "$HERE/clouds.yaml"
    echo "    Password written to clouds.yaml."
fi

echo ""
echo "========================================================"
echo " Setup complete! Next steps:"
echo ""
echo "  1. Verify cloud connectivity:"
echo "     ./check_cloud.py ./clouds.yaml"
echo ""
echo "  2. If 'ext-net' is wrong, export the correct name:"
echo "     export NSO_EXTERNAL_NETWORK='<name>'"
echo "     export NSO_FLAVOR='<name>'   # if m1.small not available"
echo ""
echo "  3. Deploy:"
echo "     ./install ./clouds.yaml raghu1 ~/.ssh/nso_id"
echo ""
echo "  4. Operate (keep running in a second terminal):"
echo "     ./operate ./clouds.yaml raghu1 ~/.ssh/nso_id"
echo ""
echo "  5. Benchmark (while operate is running):"
echo "     ./ab_bench.sh <proxy-public-ip>"
echo ""
echo "  6. Cleanup:"
echo "     ./cleanup ./clouds.yaml raghu1 ~/.ssh/nso_id"
echo "========================================================"
