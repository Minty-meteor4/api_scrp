#!/bin/bash
# =============================================================================
# VPS Deployment Script for API Key Scanner
# =============================================================================
#
# Usage (from your LOCAL machine):
#   1. scp -r /path/to/apikeys2 root@YOUR_VPS_IP:~/scanner
#   2. ssh root@YOUR_VPS_IP 'cd ~/scanner && bash deploy.sh'
#
# Or all-in-one:
#   scp -r . root@YOUR_VPS_IP:~/scanner && ssh root@YOUR_VPS_IP 'cd ~/scanner && bash deploy.sh'
#
# After deploy:
#   - Check status:  systemctl status scanner
#   - View logs:     journalctl -u scanner -f
#   - View log file: tail -f ~/scanner/scanner.log
#   - Stop:          systemctl stop scanner
#   - Restart:       systemctl restart scanner
# =============================================================================

set -e

echo "=== API Key Scanner VPS Deployment ==="

# 1. Install system deps
echo "[1/5] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv > /dev/null 2>&1

# 2. Create venv and install Python deps
echo "[2/5] Setting up Python environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# 3. Check .env exists
if [ ! -f .env ]; then
    echo ""
    echo "WARNING: .env file not found!"
    echo "Create it with at least:"
    echo "  GITHUB_TOKENS=ghp_your_token_here"
    echo ""
    echo "Example:"
    echo '  echo "GITHUB_TOKENS=ghp_xxx,ghp_yyy" > .env'
    echo ""
fi

# 4. Install systemd service
echo "[3/5] Installing systemd service..."
SCANNER_DIR=$(pwd)
# Update WorkingDirectory and ExecStart paths in service file
sed "s|/root/scanner|${SCANNER_DIR}|g" scanner.service > /etc/systemd/system/scanner.service

# 5. Enable and start
echo "[4/5] Enabling and starting service..."
systemctl daemon-reload
systemctl enable scanner
systemctl restart scanner

echo "[5/5] Done!"
echo ""
echo "=== Scanner is running ==="
echo "  Status:    systemctl status scanner"
echo "  Logs:      journalctl -u scanner -f"
echo "  Log file:  tail -f ${SCANNER_DIR}/scanner.log"
echo "  Stop:      systemctl stop scanner"
echo "  Restart:   systemctl restart scanner"
echo ""
systemctl status scanner --no-pager
