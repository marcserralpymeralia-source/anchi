# Entrega de fase

## 1. Objetivo

Preparar en la rama actual la configuración tenant-scoped de perfiles de
proxy y una primera infraestructura remota persistente, sin activar todavía
ninguna conexión hacia bases de datos.

## 2. Alcance ejecutado

- Añadido el módulo de configuración `Proxies` dentro de Ajustes.
- Añadidos perfiles con host/IP, puerto, tipo de proxy, credenciales del
  proxy, TLS, notas y estado habilitado. No se guardan destino, nombre,
  usuario ni credenciales de ninguna base de datos.
- Contraseñas cifradas con el mecanismo existente de Anchi.
- Añadidos permisos para Administrador y Superadmin, con aislamiento por
  `company_id`.
- Añadidas las migraciones tenant `2026.09.04.3` y `2026.09.04.4`; la segunda
  elimina los campos de destino de BBDD que tenía el prototipo inicial.
- Preparado un gateway remoto en una carpeta aislada bajo `/root/anchi-proxy`.
- El gateway escucha únicamente en loopback, responde a `/health` y rechaza
  cualquier tráfico de datos con `503`.
- Servicio instalado y habilitado con systemd para reinicio automático.

## 3. Alcance no ejecutado

- No se ha conectado ninguna base de datos real.
- No se han abierto puertos públicos ni se ha configurado un dominio.
- No se ha implementado todavía forwarding, autenticación de gateway,
  allowlist, TLS/mTLS ni el adaptador que en el futuro consumirá el módulo de
  BBDD.

## 4. Diagnóstico previo

La aplicación ya disponía de cifrado de secretos, configuración modular,
permisos por rol y un registro de migraciones tenant. Se reutilizaron esos
mecanismos para evitar una vía paralela de configuración.

## 5. Cambios realizados

El perfil de proxy es solo configuración. Aunque se marque como habilitado,
ningún consumidor de Anchi lo utiliza todavía. El botón de prueba queda
desactivado en la interfaz y la ruta protegida devuelve `501` sin realizar
operaciones de red.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `backend/app/db/models.py` | Modelo tenant de perfiles de proxy | Modelo SQLAlchemy |
| `backend/app/migrations/registry.py` | Crear la tabla en tenants existentes | Migración versionada |
| `backend/app/settings/routes.py` | Catálogo, dashboard, endpoints y validación | Backend |
| `backend/app/templates/settings/index.html` | Drawer y formularios de Proxies | Interfaz |
| `backend/tests/test_setup_onboarding.py` | Cubrir el módulo y el cifrado | Test relacionado |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `infra/anchi-proxy/proxy_gateway.py` | Gateway remoto inactivo, sin forwarding |
| `infra/anchi-proxy/config.env.example` | Configuración segura de ejemplo |
| `infra/anchi-proxy/anchi-proxy.service` | Unidad systemd persistente |
| `infra/anchi-proxy/README.md` | Instalación y criterios de activación futura |
| `docs/development/proxy-gateway-phase-delivery.md` | Registro de la entrega |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Perfil tenant-scoped | Evita mezclar configuración de proxy entre empresas | Configuración global compartida |
| Contraseña cifrada | Reutiliza la protección existente | Texto plano o variables en HTML |
| Gateway local-only | No hay dominio, TLS ni módulo consumidor listo | Exponer HTTP directo en la IP pública |
| Gateway stdlib | No añade dependencias al servidor remoto | Instalar un stack adicional para un stub |
| `systemd` con `Restart=always` | Persistencia tras reinicio del proceso/host | Proceso manual o `screen` |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python -m compileall -q app` con Python 3.14 del entorno local | Correcto |
| Test directo de `_proxy_form_values` | Hosts válidos aceptados y URL/ruta/espacios rechazados |
| Test directo de `_apply_tenant_proxy_connections` y `_apply_tenant_proxy_connection_scope` | Tabla creada y prototipo antiguo convertido a campos solo de proxy |
| `python -m unittest tests.test_schema_migrations` | 21 tests OK |
| `systemd-analyze verify /etc/systemd/system/anchi-proxy.service` | Correcto |
| `systemctl is-enabled anchi-proxy.service` | `enabled` |
| `systemctl is-active anchi-proxy.service` | `active` |
| `curl http://127.0.0.1:8787/health` | HTTP 200; `traffic_enabled=false` |
| `curl http://127.0.0.1:8787/v1/test` | HTTP 503; sin forwarding |
| `systemctl restart anchi-proxy.service` + healthcheck | Correcto; vuelve a quedar activo |
| `ss -ltnp` sobre el puerto del gateway | Listener únicamente en `127.0.0.1` |
| `git diff --check` | Sin errores de whitespace en el diff |

## 10. Tests añadidos o modificados

Se añadió cobertura del módulo de configuración, cifrado de contraseña,
renderizado del fragmento y respuesta `501` del endpoint de prueba. La suite
completa de `test_setup_onboarding.py` no se pudo ejecutar porque contiene un
cambio local ajeno a esta fase con una línea con indentación inválida.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Configuración por tenant | Cumplido | Consulta y endpoints filtran por `company_id` |
| Secretos no visibles ni en claro | Cumplido | `password_encrypted` y test de descifrado |
| Sin conexiones de datos desde Anchi | Cumplido | No hay llamada de red en el módulo; prueba `501` |
| Migración compatible | Cumplido | Migraciones registradas; conversión de la tabla antigua cubierta en test |
| Servicio remoto resistente a reinicios | Cumplido | systemd enabled/active y restart validado |
| Exposición pública segura | Pendiente | Requiere TLS/mTLS y política de acceso antes de activar forwarding |

## 12. Riesgos y observaciones pendientes

- El gateway corre como servicio aislado y no debe exponerse públicamente en
  HTTP antes de incorporar autenticación y TLS/mTLS.
- La contraseña SSH compartida para esta preparación debe rotarse antes de
  cualquier uso continuado.
- El test local de onboarding debe corregirse en el cambio que lo introdujo;
  no se ha sobrescrito para preservar cambios locales ajenos.

## 13. Desviaciones respecto al alcance inicial

El gateway queda deliberadamente limitado a healthcheck y rechazo de tráfico.
Esto mantiene el requisito de no pasar todavía peticiones por el proxy hasta
que exista el módulo de destino y evita activar una ruta insegura por defecto.

## 14. Estado final de Git

La rama actual es `chore/technical-improvement-plan`. No se ha hecho commit ni
push porque no se solicitó en esta fase. El árbol contiene cambios locales
previos ajenos al proxy, que se han conservado sin descartar ni mezclar.

## 15. Recomendación para la siguiente fase

Implementar primero el contrato autenticado del gateway (identidad de tenant,
allowlist, TLS/mTLS, límites, timeouts y auditoría sin secretos). Después
añadir un adaptador de destino con consultas estrictamente permitidas y
pruebas de aislamiento antes de activar el primer perfil.
