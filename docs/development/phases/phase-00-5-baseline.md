# Fase 0.5 - Linea base del repositorio

## 1. Objetivo

Crear el primer commit del repositorio con una linea base Git limpia, reproducible y segura para empezar la Fase 1 sin arrastrar datos locales ni artefactos generados.

## 2. Estado inicial

- Rama activa: `chore/technical-improvement-plan`
- Repositorio sin commits iniciales
- Arbol completo mayoritariamente `untracked`
- Sin remotos configurados
- Entorno virtual localizado en `backend/.venv`
- Bases locales detectadas en raiz y en `backend/`
- Documentacion de desarrollo ya creada en `docs/development/`
- `AGENTS.md` presente en la raiz

## 3. Reglas reforzadas en `.gitignore`

- Cachés de Python y herramientas de desarrollo
- Entornos virtuales
- Ficheros `.env` y variantes locales
- Claves, certificados y secretos locales
- Bases de datos SQLite
- Logs y temporales
- Almacenamiento de ejecucion y datos generados
- Adjuntos y exportaciones locales
- Ficheros del IDE y del sistema operativo
- Ficheros auxiliares generados por Codex

## 4. Archivos excluidos

- `backend/.env`
- `backend/.venv/`
- `backend/anchi_demo.db`
- `backend/dialma.db`
- `backend/gemavi.db`
- `backend/master.db`
- `backend/order_agent.db`
- `master.db`
- `order_agent.db`
- `anchi_demo.db`
- `clients/dialma.env`
- `clients/gemavi.env`
- `backend/storage/`
- `backend/app/storage/`
- `backend/storage/attachments/`
- `backend/app/storage/attachments/`
- `backend/app/storage/import_previews/`
- bytecode y `__pycache__`
- `_codex_write_test.txt`

## 5. Archivos incluidos

- Codigo fuente de `backend/app/`
- Tests de `backend/tests/`
- `backend/requirements.txt`
- `backend/.env.example`
- `clients/.gitkeep`
- `clients/example.env.example`
- `README.md`
- `ARCHITECTURE.md`
- `CLIENTE_NUEVO.md`
- `ESCALAR_CLIENTES.md`
- `AGENTS.md`
- `scripts/`
- `docs/`
- `.gitignore`

## 6. Comprobaciones de secretos

- Se detectaron ficheros reales de entorno local en `backend/.env` y en `clients/*.env`
- Se mantuvieron fuera del versionado
- No se mostraron valores completos de credenciales reales
- Los ejemplos de entorno se conservaron solo donde eran ficheros de plantilla

## 7. Validaciones ejecutadas

- `./.venv/bin/python -c "from app.main import app; print(app.title)"`
- `./.venv/bin/python -m compileall app`
- `./.venv/bin/python -m unittest tests.test_core`
- `git status --short`
- `git status --ignored --short`
- `git check-ignore -v backend/.venv backend/master.db backend/anchi_demo.db backend/.env`

## 8. Hash del commit

- `b179b2d`

## 9. Estado final esperado

- Un solo commit inicial
- Sin push
- Sin remoto
- Sin bases de datos, secretos ni entorno virtual en Git
- Con un punto de comparacion valido para fases posteriores

## 10. Pendientes o dudosos

- `backend/.env.example` y `clients/example.env.example` quedan como plantillas seguras
- Los datos operativos reales siguen siendo locales y excluidos
