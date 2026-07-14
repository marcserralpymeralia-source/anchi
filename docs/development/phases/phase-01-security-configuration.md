# Fase 1 - Seguridad de configuracion y despliegue

## 1. Objetivo

Endurecer la configuracion de arranque para que produccion no pueda ejecutarse con secretos debiles, demo bootstrap, cookies inseguras o host/origenes abiertos.

## 2. Diagnostico previo

| Area | Configuracion actual | Riesgo | Archivo | Cambio necesario |
| --- | --- | --- | --- | --- |
| Entorno | Se deducia por defecto desde `development` sin validacion explicita | Un despliegue podia heredar defaults de desarrollo | `backend/app/core/config.py` | Introducir `APP_ENV` y validarlo |
| Secretos | `SECRET_KEY` y claves demo por defecto | Claves reutilizables o debiles | `backend/app/core/config.py`, `backend/.env.example` | Rechazar secretos inseguros en produccion |
| Cifrado | Una unica clave implícita para secretos | Mezcla de responsabilidades y riesgo de reutilizacion | `backend/app/core/encryption.py` | Separar y validar la clave de cifrado |
| Sesion | Cookies sin endurecimiento por entorno | Sesiones menos seguras en produccion | `backend/app/core/app_factory.py` | Configurar `https_only`, `same_site` y `max_age` |
| CORS | Lista fija de orígenes locales | Posible configuracion demasiado abierta o poco explícita | `backend/app/core/app_factory.py` | Leer origenes desde configuracion y evitar `*` |
| Hosts | No habia restriccion explicita | Riesgo de Host header abierto | `backend/app/core/app_factory.py` | Añadir `TrustedHostMiddleware` |
| Login | Credenciales demo precargadas | Exposición de datos iniciales en UI | `backend/app/templates/login.html` | Eliminar autocompletado de credenciales demo |
| Diagnosticos | Se devolvia la clave de base tenant en respuestas técnicas | Fuga de identificadores sensibles | `backend/app/admin/diagnostics.py`, `backend/app/health/routes.py` | Redactar esa salida |

## 3. Alcance ejecutado

- Validacion fuerte de entorno, secretos, cookies, hosts y CORS.
- Separacion de clave de cifrado respecto a la clave de sesion.
- Redaccion de secretos en representacion textual.
- Eliminacion de credenciales demo precargadas en login.
- Alineacion de la documentacion de despliegue.
- Nuevos tests de configuracion segura.

## 4. Archivos previstos

- `backend/app/core/config.py`
- `backend/app/core/app_factory.py`
- `backend/app/core/encryption.py`
- `backend/app/templates/login.html`
- `backend/app/admin/diagnostics.py`
- `backend/app/health/routes.py`
- `backend/.env.example`
- `backend/tests/test_security_config.py`
- `docs/development/*`

## 5. Archivos modificados

`d96f731` - `security: harden environment configuration`

## 6. Decisiones tecnicas

- Mantener compatibilidad con los nombres historicos mediante aliases de entorno.
- No introducir nuevas dependencias.
- Validar en el arranque y no con fallbacks silenciosos.
- Mantener desarrollo y test funcionales sin obligar a variables de produccion.

## 7. Validaciones

- `./.venv/bin/python -c "from app.main import app; print(app.title)"`
- `./.venv/bin/python -m compileall app`
- `./.venv/bin/python -m unittest discover -s tests`
- `./.venv/bin/python -m unittest tests.test_security_config`

## 8. Criterios de aceptacion

- Produccion rechaza secretos debiles.
- Produccion rechaza demo bootstrap y debug.
- Produccion usa cookies seguras.
- Produccion exige hosts y origenes explicitos.
- Los secretos no aparecen en la UI ni en diagnosticos.
- El desarrollo local sigue arrancando.

## 9. Riesgos pendientes

- La base de codigo sigue teniendo mucha superficie funcional fuera de esta fase.
- Hay configuraciones historicas con nombres antiguos que se mantienen por compatibilidad.

## 10. Desviaciones

- No se modifico el modelo de datos.
- No se crearon migraciones.
- No se abordaron rutas, jobs ni arquitectura funcional fuera de la configuracion.
