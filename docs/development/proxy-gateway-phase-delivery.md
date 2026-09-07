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
- El gateway escucha solo en loopback del servidor remoto, responde a
  `/health` con autenticación Basic y rechaza cualquier tráfico de datos con
  `503`.
- Servicio instalado y habilitado con systemd para reinicio automático.
- Se ha creado en el servidor remoto el usuario técnico `anchi-proxy-demo`.
  Su contraseña aleatoria queda solo en `/root/anchi-proxy/config.env` con
  permisos `600` y no se copia al repositorio ni a este documento.

## 3. Alcance no ejecutado

- No se ha conectado ninguna base de datos real.
- No se expone `8787` públicamente. Apache publica únicamente `/health` en
  `https://suspicious-raman.82-223-17-129.plesk.page/health`, usando el
  certificado válido de Let's Encrypt del hostname y reenviando al gateway
  local.
- No se ha implementado todavía forwarding, allowlist, mTLS entre servicios ni
  el adaptador que en el futuro consumirá el módulo de BBDD. El TLS público
  del healthcheck sí termina en Apache.

## 4. Diagnóstico previo

La aplicación ya disponía de cifrado de secretos, configuración modular,
permisos por rol y un registro de migraciones tenant. Se reutilizaron esos
mecanismos para evitar una vía paralela de configuración.

## 5. Cambios realizados

El perfil de proxy es solo configuración. Aunque se marque como habilitado,
ningún consumidor de Anchi lo utiliza todavía. El botón de prueba consulta
exclusivamente `GET /health` del gateway configurado, persiste el resultado y
no envía destinos, consultas ni tráfico de base de datos.

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
| `infra/anchi-proxy/apache-health-vhost.conf.example` | Plantilla del reverse proxy HTTPS |
| `infra/anchi-proxy/README.md` | Instalación y criterios de activación futura |
| `docs/development/proxy-gateway-phase-delivery.md` | Registro de la entrega |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Perfil tenant-scoped | Evita mezclar configuración de proxy entre empresas | Configuración global compartida |
| Contraseña cifrada | Reutiliza la protección existente | Texto plano o variables en HTML |
| Healthcheck autenticado y sin forwarding | Permite probar que el gateway remoto está vivo sin tocar BBDD | Activar forwarding antes de tener TLS, aislamiento y allowlist |
| Gateway stdlib | No añade dependencias al servidor remoto | Instalar un stack adicional para un stub |
| `systemd` con `Restart=always` | Persistencia tras reinicio del proceso/host | Proceso manual o `screen` |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `python -m compileall -q app` con Python 3.14 del entorno local | Correcto |
| Test directo de `_proxy_form_values` | Hosts válidos aceptados y URL/ruta/espacios rechazados |
| Test directo de `_apply_tenant_proxy_connections` y `_apply_tenant_proxy_connection_scope` | Tabla creada y prototipo antiguo convertido a campos solo de proxy |
| `python -m unittest tests.test_schema_migrations` | 22 tests OK |
| `systemd-analyze verify /etc/systemd/system/anchi-proxy.service` | Correcto |
| `systemctl is-enabled anchi-proxy.service` | `enabled` |
| `systemctl is-active anchi-proxy.service` | `active` |
| `curl` local sin credenciales al healthcheck | HTTP 401 |
| `curl` local autenticado al healthcheck | HTTP 200; `traffic_enabled=false` |
| `curl` autenticado a `/v1/test` | HTTP 503; sin forwarding |
| `systemctl restart anchi-proxy.service` + healthcheck | Correcto; vuelve a quedar activo |
| `ss -ltnp` sobre el puerto del gateway | Listener únicamente en `127.0.0.1:8787` |
| HTTPS sin credenciales al hostname | HTTP 401; el reverse proxy llega al gateway |
| HTTPS autenticado desde el servidor | HTTP 200; `traffic_enabled=false` |
| HTTPS autenticado a un endpoint de datos | HTTP 403; no existe ruta de forwarding |
| `git diff --check` | Sin errores de whitespace en el diff |

## 10. Tests añadidos o modificados

Se añadió cobertura del módulo de configuración, cifrado de contraseña,
renderizado del fragmento y healthcheck autenticado aislado de cualquier
destino de datos.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Configuración por tenant | Cumplido | Consulta y endpoints filtran por `company_id` |
| Secretos no visibles ni en claro | Cumplido | `password_encrypted` y test de descifrado |
| Sin conexiones de datos desde Anchi | Cumplido | Solo se consulta `/health`; los endpoints de datos siguen bloqueados |
| Migración compatible | Cumplido | Migraciones registradas; conversión de la tabla antigua cubierta en test |
| Servicio remoto resistente a reinicios | Cumplido | systemd enabled/active y restart validado |
| Exposición pública del healthcheck | Cumplido para demo | HTTPS válido, Basic Auth y solo `/health`; el forwarding productivo sigue pendiente |

## 12. Riesgos y observaciones pendientes

- La escucha pública no expone el proceso ni el puerto 8787: solo publica el
  healthcheck por HTTPS. La contraseña debe considerarse temporal y rotarse.
- El forwarding de BBDD continúa desactivado y no debe activarse sin identidad
  de tenant, allowlist, límites y pruebas de aislamiento.
- La contraseña SSH compartida para esta preparación debe rotarse antes de
  cualquier uso continuado.
- El test local de onboarding debe corregirse en el cambio que lo introdujo;
  no se ha sobrescrito para preservar cambios locales ajenos.

## 13. Desviaciones respecto al alcance inicial

El gateway queda deliberadamente limitado a healthcheck autenticado y rechazo
de tráfico. Esto mantiene el requisito de no pasar todavía peticiones por el
proxy hasta que exista el módulo de destino y evita activar una ruta insegura
por defecto.

## 14. Estado final de Git

La rama actual es `chore/technical-improvement-plan`. No se ha hecho commit ni
push porque no se solicitó en esta fase. El árbol contiene cambios locales
previos ajenos al proxy, que se han conservado sin descartar ni mezclar.

## 15. Recomendación para la siguiente fase

Antes de implementar forwarding, cerrar el contrato autenticado del gateway
(identidad de tenant, allowlist, mTLS, límites, timeouts y auditoría sin
secretos). Después añadir un adaptador de destino con consultas estrictamente
permitidas y pruebas de aislamiento antes de activar el primer perfil.
