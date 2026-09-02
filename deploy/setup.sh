#!/bin/bash
# Deploy algo-strategy as a systemd service on Oracle Cloud VM
# Run this once after cloning the repo and configuring .env

set -e

echo "Installing systemd service..."
sudo cp deploy/algo-strategy.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable algo-strategy
sudo systemctl start algo-strategy

echo ""
echo "Done! Bot is running as a service."
echo ""
echo "Useful commands:"
echo "  sudo systemctl status algo-strategy    # check status"
echo "  sudo journalctl -u algo-strategy -f    # live logs"
echo "  sudo systemctl restart algo-strategy   # restart"
echo "  sudo systemctl stop algo-strategy      # stop"
