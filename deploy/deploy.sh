#!/bin/bash
set -euo pipefail

PROJECT_PATH="/var/www/marketplace-hub"
VENV_PATH="$PROJECT_PATH/.venv"

cd "$PROJECT_PATH"
mkdir -p logs

echo "📦 Updating code..."
git fetch https://github.com/noughitarek/marketplace-hub.git main
git reset --hard FETCH_HEAD   # .env is gitignored, so it is kept

[ -d "$VENV_PATH" ] || python3 -m venv "$VENV_PATH"
source "$VENV_PATH/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt
pip install gunicorn

sudo chown -R www-data:www-data "$PROJECT_PATH"

sudo systemctl daemon-reload
sudo systemctl enable marketplace-hub
sudo systemctl restart marketplace-hub

for i in $(seq 1 30); do
  curl -s --max-time 2 -o /dev/null http://127.0.0.1:5006/ && { echo "✅ Up on 5006"; exit 0; }
  sleep 1
done
echo "❌ Not responding"
sudo journalctl -u marketplace-hub -n 80 --no-pager
exit 1