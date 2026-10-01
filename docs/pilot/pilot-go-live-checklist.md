# Checklist de go-live del piloto

Responsable de aprobación: ____________________

Fecha/ventana: ____________________

Entorno/commit: ____________________
Decisión: **GO / NO GO / GO CONDICIONADO**

## 1. Alcance y datos

- [ ] El contrato sólo incluye WhatsApp Catalog estructurado → un destino.
- [ ] Email, audio, autodiscovery y adjuntos están desactivados o explícitamente excluidos.
- [ ] Tenant(s), usuarios, catálogo, mappings y owner operativo están aprobados.
- [ ] No se han cargado datos de producción innecesarios.
- [ ] SLA de recepción, revisión, entrega y soporte está firmado.

## 2. Seguridad y acceso

- [ ] Secrets están en secret manager y no en repo, imagen, logs o tickets.
- [ ] `APP_ENV`, `DEBUG`, cookies, hosts, CORS y claves cumplen el checklist de entorno.
- [ ] RBAC probado para operador, supervisor, administrador y superadmin.
- [ ] Cuentas de soporte son nominativas; MFA/rotación según política disponible.
- [ ] Plan de revocación y offboarding aprobado.

## 3. Datos, migrations y recovery

- [ ] Backup/snapshot previo y restore de staging validados.
- [ ] Master y todos los tenants reportan schema actual y checksum esperado.
- [ ] No hay migraciones pendientes o incompatibles.
- [ ] RPO/RTO, retención y responsable de backup constan en el acta.
- [ ] Rollback no depende de downgrade automático.

## 4. Storage y adjuntos

- [ ] Storage persistente/Blob privado provisionado.
- [ ] Escribir, leer y reiniciar conservando un objeto sintético.
- [ ] Cuota, permisos, cifrado y retención comprobados.
- [ ] Readiness refleja fallo de storage en lugar de afirmar siempre `true`.
- [ ] Si el alcance Catalog-only no recibe adjuntos, el canal rechaza/excluye media de forma visible.

## 5. Flujo funcional

- [ ] Webhook válido crea un inbound y un pedido estructurado.
- [ ] Repetir el mismo evento no crea otro pedido, outbox o job.
- [ ] Cliente/producto desconocido queda en revisión sin entrega.
- [ ] Duda corregida en workbench deja actor, motivo y estado auditados.
- [ ] El procedimiento de confirmar y exportar produce exactamente una entrega.
- [ ] Entrega exitosa conserva `external_order_id`.
- [ ] Timeout/health/rate limit reintenta con backoff y sin duplicar.
- [ ] Éxito remoto + timeout local reconcilia correctamente.
- [ ] Error permanente queda visible y no se reintenta indefinidamente.

## 6. Worker, cron y operaciones

- [ ] Web y worker tienen configuración idéntica de DB/tenant/encryption/storage.
- [ ] Sólo existe un worker activo por contrato de entorno.
- [ ] Backlog, stale jobs, retries y worker heartbeat tienen alerta.
- [ ] Cron jobs ejecutan con secreto y existe prueba de 401/403.
- [ ] Soporte puede localizar y recuperar una entrega fallida sin editar DB directamente.
- [ ] El runbook de [pilot-support-runbook.md](./pilot-support-runbook.md) está disponible para el turno.

## 7. Observabilidad y aceptación browser

- [ ] Se han guardado health snapshots antes y después del smoke.
- [ ] Se ha trazado un evento sintético completo con IDs y correlation ID.
- [ ] Logs redaccionan secretos y PII no necesaria.
- [ ] Browser smoke en los navegadores soportados: login, selección tenant, inbox, workbench, confirmación, exportación, jobs, logout.
- [ ] No hay errores de consola o respuestas 5xx inesperadas.

## 8. Smoke final y stop test

- [ ] Ejecutar un pedido válido de catálogo.
- [ ] Ejecutar un duplicado.
- [ ] Ejecutar un cliente desconocido y resolverlo manualmente.
- [ ] Provocar un timeout del destino y verificar retry/recovery.
- [ ] Parar worker y comprobar que el incidente es detectable y recuperable.
- [ ] Verificar que el procedimiento de parada/rollback está entendido por el on-call.

## Criterio de no-go automático

Marcar **NO GO** ante cualquier cruce de tenant, secreto expuesto, storage no persistente, backup no restaurable, migration incompatible, duplicado externo no reconciliable, worker/cron sin propietario, o fallo no clasificado en la suite de release.
