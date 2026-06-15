#!/bin/sh
set -e

echo "Initializing database..."
python -c "from app.db.models import init_db; init_db(); print('DB ready')"

exec uvicorn app.main:app --host 0.0.0.0 --port 8000
