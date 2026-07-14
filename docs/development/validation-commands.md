# Comandos de validacion

| Accion | Directorio | Comando | Estado | Observaciones |
|---|---|---|---|---|
| Instalacion | `backend/` | `./.venv/bin/python -m pip install -r requirements.txt` | Detectado | Basado en `backend/requirements.txt` |
| Ejecucion | `backend/` | `./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000` | Detectado | Requiere el venv local |
| Tests | `backend/` | `./.venv/bin/python -m unittest tests.test_core` | Verificado | 9 tests OK |
| Test individual | `backend/` | `No configurado actualmente` | No configurado actualmente | No se identifico un test unitario mas granular |
| Compilacion | `backend/` | `./.venv/bin/python -m compileall app` | Verificado | Sin errores |
| Lint | `backend/` | `No configurado actualmente` | No configurado actualmente | No se encontro Ruff/Flake8/Pylint |
| Tipado | `backend/` | `No configurado actualmente` | No configurado actualmente | No se encontro mypy/pyright/basedpyright |
| Migraciones | `backend/` | `No configurado actualmente` | No configurado actualmente | La inicializacion es automatica y por tenant |
| Worker email | `backend/` | `No configurado actualmente` | No configurado actualmente | Existe modulo, pero no comando CLI estable verificado |
| Worker jobs | `backend/` | `No configurado actualmente` | No configurado actualmente | Existe modulo, pero no comando CLI estable verificado |

