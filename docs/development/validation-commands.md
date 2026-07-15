# Comandos de validacion

| Accion | Directorio | Comando | Estado | Observaciones |
|---|---|---|---|---|
| Instalacion | `backend/` | `./.venv/bin/python -m pip install -r requirements.txt` | Detectado | Basado en `backend/requirements.txt` |
| Ejecucion | `backend/` | `./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000` | Detectado | Requiere el venv local |
| Tests | `backend/` | `./.venv/bin/python -m unittest tests.test_core` | Verificado | 9 tests OK |
| Tests jobs | `backend/` | `APP_ENV=test ./.venv/bin/python -m unittest tests.test_jobs_reliability` | Verificado | Cobertura de idempotencia, retries, recovery y worker |
| Tests observabilidad | `backend/` | `APP_ENV=test ./.venv/bin/python -m unittest tests.test_observability` | Verificado | Cubre contexto, trazas, health y diagnosticos |
| Tests de seguridad | `backend/` | `./.venv/bin/python -m unittest tests.test_security_config` | Pendiente | Cobertura nueva de entorno y config |
| Suite completa | `backend/` | `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests` | Verificado | 42 tests OK |
| Test individual | `backend/` | `No configurado actualmente` | No configurado actualmente | No se identifico un test unitario mas granular |
| Tests rendimiento | `backend/` | `APP_ENV=test ./.venv/bin/python -m unittest tests.test_performance_instrumentation` | Verificado | Cubre profiling, repeticion de consultas, fixtures temporales y script |
| Compilacion | `backend/` | `./.venv/bin/python -m compileall app` | Verificado | Sin errores |
| Arranque local | `backend/` | `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"` | Verificado | Import y arranque base OK |
| Benchmark pequeno | `backend/` | `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario small` | Verificado | Genera JSON/CSV en `backend/performance-results/` |
| Benchmark medio | `backend/` | `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario medium` | Verificado | Escenario comparativo de volumen medio |
| Benchmark grande | `backend/` | `APP_ENV=test ./.venv/bin/python scripts/measure_performance.py --scenario large` | Verificado | Escenario comparativo de volumen alto |
| Rechazo prod inseguro | `backend/` | `APP_ENV=production SECRET_KEY=demo ENCRYPTION_KEY=demo ./.venv/bin/python -c "from app.core.config import get_settings; get_settings.cache_clear(); get_settings()"` | Pendiente | Debe fallar sin secretos validos |
| Lint | `backend/` | `No configurado actualmente` | No configurado actualmente | No se encontro Ruff/Flake8/Pylint |
| Tipado | `backend/` | `No configurado actualmente` | No configurado actualmente | No se encontro mypy/pyright/basedpyright |
| Migraciones | `backend/` | `No configurado actualmente` | No configurado actualmente | La inicializacion es automatica y por tenant |
| Worker email | `backend/` | `APP_ENV=development ./.venv/bin/python -m app.workers.email_worker` | Documentado | Worker separado para IMAP/sincronizacion |
| Worker jobs | `backend/` | `APP_ENV=development ./.venv/bin/python -m app.workers.jobs_worker` | Verificado | Entrada estable con recovery y cola idempotente |
| Health live | `backend/` | `curl http://127.0.0.1:8000/health/live` | Documentado | Sin dependencia de base |
| Health ready | `backend/` | `curl http://127.0.0.1:8000/health/ready` | Documentado | Valida master, tenant y metrics |
| Metrics | `backend/` | `curl http://127.0.0.1:8000/health/metrics` | Documentado | Snapshot interno |
