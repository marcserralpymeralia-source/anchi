web: cd backend && APP_ENV=production uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: cd backend && APP_ENV=production python -m app.workers.jobs_worker

