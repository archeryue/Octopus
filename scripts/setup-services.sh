#!/bin/bash
set -e

# Create octopus service
sudo tee /etc/systemd/system/octopus.service > /dev/null << 'EOF'
[Unit]
Description=Octopus Server
After=network.target

[Service]
Type=simple
User=start-up
WorkingDirectory=/home/start-up
EnvironmentFile=/home/start-up/Octopus/.env
ExecStart=/home/start-up/Octopus/.venv/bin/octopus serve
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Create cloudflared service
sudo tee /etc/systemd/system/cloudflared.service > /dev/null << 'EOF'
[Unit]
Description=Cloudflare Tunnel
After=network.target

[Service]
Type=simple
User=start-up
ExecStart=/home/start-up/.local/bin/cloudflared tunnel run octopus
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable octopus cloudflared
sudo systemctl start octopus cloudflared

echo "Done! Checking status..."
sudo systemctl status octopus cloudflared --no-pager
