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

# 4. Install systemd services
echo "[3/6] Installing systemd services..."
SCANNER_DIR=$(pwd)
sed "s|/root/scanner|${SCANNER_DIR}|g" scanner.service > /etc/systemd/system/scanner.service
sed "s|/root/scanner|${SCANNER_DIR}|g" telegram_bot.service > /etc/systemd/system/telegram_bot.service

# 5. Enable and start scanner
echo "[4/6] Enabling and starting scanner..."
systemctl daemon-reload
systemctl enable scanner
systemctl restart scanner

# 6. Start Telegram bot (if configured)
echo "[5/6] Starting Telegram bot..."
if grep -q "TELEGRAM_BOT_TOKEN" .env 2>/dev/null; then
    systemctl enable telegram_bot
    systemctl restart telegram_bot
    echo "  Telegram bot started!"
else
    echo "  Skipped (no TELEGRAM_BOT_TOKEN in .env)"
fi

echo "[6/6] Done!"
echo ""
echo "=== Services running ==="
echo "  Scanner:    systemctl status scanner"
echo "  TG Bot:     systemctl status telegram_bot"
echo "  Logs:       tail -f ${SCANNER_DIR}/scanner.log"
echo "  TG Log:     tail -f ${SCANNER_DIR}/telegram_bot.log"
echo "  Stop:       systemctl stop scanner telegram_bot"
echo "  Restart:    systemctl restart scanner telegram_bot"
echo ""
systemctl status scanner --no-pager
echo ""
systemctl status telegram_bot --no-pager 2>/dev/null || true
