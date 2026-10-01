# Runbook de backup y restore del piloto

Fecha: 2026-10-01  
Ámbito: ANCHI-GEMAVI, Block 12, entorno sintético/offline  
Propietario: Plataforma + Backend

## Objetivo y límites

Este runbook define una operación repetible y auditable para proteger las bases del piloto y demostrar que una copia puede restaurarse sin romper invariantes de negocio. No autoriza el uso de datos de producción ni sustituye la política de backup del proveedor gestionado.

La operación final debe cubrir, como mínimo, la base master y cada base tenant. PostgreSQL es el motor de referencia: se usa `pg_dump --format=custom --no-owner` y `pg_restore --exit-on-error --no-owner`. SQLite se utiliza únicamente para rehearsal aislado y tests offline.

## Metadata obligatoria

Cada artefacto debe conservar junto a la copia un sidecar JSON con:

- `created_at` en UTC;
- `environment` (`test`, `staging` o el nombre aprobado);
- `database_identity` (`master` o `tenant:<id>/<slug>`);
- `engine`;
- nombre del artefacto;
- `sha256`;
- versión/checksum/estado de esquema cuando el motor lo expone.

No se escriben passwords en argumentos, logs ni metadata. Para PostgreSQL la contraseña se entrega a `pg_dump`/`pg_restore` mediante `PGPASSWORD` construido sólo en el entorno del proceso; en staging/prod debe preferirse el mecanismo de secretos del proveedor.

## Backup PostgreSQL

1. Congelar el alcance temporal del rehearsal y registrar commit, entorno, RPO/RTO y responsable.
2. Comprobar `/health/ready` y el estado operativo. Si hay `database`, `storage`, `worker` o scheduler requerido en estado no saludable, registrar la excepción antes de continuar.
3. Crear una copia por identidad, fuera del volumen de la base:

```bash
PYTHONPATH=backend python3 backend/scripts/pilot_backup_restore.py backup \
  --database-url "$MASTER_DATABASE_URL" \
  --output /secure-backups/2026-10-01/master.dump \
  --environment staging \
  --identity master
```

Repetir con cada `TENANT_DATABASE_URL` y `--identity tenant:<company_id>/<slug>`. La carpeta debe tener permisos restringidos, cifrado y retención aprobada.

4. Verificar que existen el dump y su sidecar `.json`, calcular el checksum publicado y registrar tamaño, timestamp, identidad y resultado.
5. No borrar la copia anterior hasta que la nueva haya pasado restore y verificación.

## Restore PostgreSQL

El restore se hace siempre en una base nueva o aislada, nunca sobre la base del piloto activo.

1. Provisionar una base destino vacía con el mismo major de PostgreSQL o una versión compatible aprobada.
2. Usar `pg_restore --exit-on-error --no-owner --dbname <destino> <artefacto>` mediante la función `pg_restore` de `app.core.backup` o el comando estándar equivalente.
3. Comparar el SHA-256 del artefacto con su sidecar antes de restaurar.
4. Ejecutar migraciones de lectura/validación sólo si el procedimiento de la versión lo exige; no hacer downgrade automático.
5. Validar los invariantes:
   - todas las filas de `orders`, `inbound_messages`, `order_deliveries`, `background_jobs` y `audit_logs` esperadas existen;
   - `external_id`/`ExternalIdentity` y `idempotency_key` no cambian;
   - una entrega `exported` no vuelve a exportarse;
   - una entrega `failed` puede entrar en retry manual sin crear una segunda entrega;
   - los estados y el historial de auditoría se conservan;
   - master mantiene compañías, membresías y mapeo a tenant;
   - la versión/checksum de esquema coincide con el artefacto o queda explícitamente clasificada.
6. Guardar un snapshot de health, el resultado de las consultas de invariantes y el responsable que aprobó el restore.

## Rehearsal SQLite offline

Para una prueba local reproducible:

```bash
PYTHONPATH=backend python3 backend/scripts/pilot_backup_restore.py backup \
  --database-url sqlite:////tmp/anchi-master.sqlite \
  --output /tmp/anchi-master.backup.sqlite \
  --environment test --identity master

PYTHONPATH=backend python3 backend/scripts/pilot_backup_restore.py verify-sqlite \
  --input /tmp/anchi-master.backup.sqlite

PYTHONPATH=backend python3 backend/scripts/pilot_backup_restore.py restore-sqlite \
  --input /tmp/anchi-master.backup.sqlite \
  --database-url sqlite:////tmp/anchi-master-restored.sqlite
```

La prueba Block 12 cubre master y tenant, checksum, integridad SQLite, identidad externa, delivery fallida/exportada, retry idempotente y audit log en `backend/tests/test_pilot_closure.py`.

## Fallo y recuperación

- Si falla el backup: conservar el último backup válido, alertar al responsable y no declarar el entorno recuperable.
- Si falla el checksum: descartar el artefacto nuevo, no restaurarlo y repetir desde la fuente.
- Si falla el restore: conservar logs y artefacto, aislar la base destino y abrir incidente; nunca reintentar destructivamente sobre una base activa.
- Si falla storage: `/health/ready` debe quedar no saludable; corregir permisos/volumen/Blob y repetir el probe de escritura/lectura.
- Si falla un invariant: el resultado es `NO GO` hasta explicar si es defecto, versión incompatible o dato sintético mal construido.

## Evidencia mínima

`backup.json`, sidecar de cada artefacto, checksum, versión de esquema, identity, health snapshots antes/después, consultas de invariantes, duración, tamaño, RPO/RTO observado y nombre del operador. Nunca incluir secretos, tokens ni payloads de producción.
