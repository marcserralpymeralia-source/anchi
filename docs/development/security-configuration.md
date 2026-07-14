# Configuracion de seguridad

## Entornos

- `development`: uso local, cookies compatibles con HTTP y defaults comodas.
- `test`: entorno aislado para tests y validacion automatizada.
- `production`: validacion estricta, sin defaults inseguros.

Si `APP_ENV` no esta definido, la app usa `development` y registra un aviso seguro.

## Variables principales

| Variable | Uso |
|---|---|
| `APP_ENV` | Selecciona el entorno. |
| `SECRET_KEY` | Firma de sesiones. |
| `ENCRYPTION_KEY` | Cifrado de secretos almacenados. |
| `DEBUG` | Control de depuracion. |
| `ENABLE_DEMO_BOOTSTRAP` | Habilita o bloquea el bootstrap demo. |
| `ALLOWED_HOSTS` | Hosts permitidos. |
| `CORS_ALLOWED_ORIGINS` | Origenes permitidos para CORS. |
| `SESSION_COOKIE_SECURE` | Cookie segura en HTTPS. |
| `SESSION_COOKIE_SAMESITE` | Politica SameSite de la cookie. |
| `SESSION_MAX_AGE` | Duracion de sesion en segundos. |
| `DATABASE_URL` | Base operativa del tenant. |
| `MASTER_DATABASE_URL` | Base master. |

## Alias historicas compatibles

- `ENVIRONMENT` -> `APP_ENV`
- `APP_SECRET_KEY` -> `SECRET_KEY`
- `SEED_DEMO_DATA` -> `ENABLE_DEMO_BOOTSTRAP`

## Reglas de produccion

- `SECRET_KEY` debe existir y no parecer demo.
- `ENCRYPTION_KEY` debe existir y ser una clave Fernet valida.
- `DEBUG` debe ser `false`.
- `ENABLE_DEMO_BOOTSTRAP` debe ser `false`.
- `SESSION_COOKIE_SECURE` debe ser `true`.
- `ALLOWED_HOSTS` debe estar definido.
- `CORS_ALLOWED_ORIGINS` no puede contener `*`.
- `DATABASE_URL` y `MASTER_DATABASE_URL` no pueden usar SQLite.
- Las credenciales demo del login no se precargan en la interfaz.

## Generacion de claves

Clave de sesion:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Clave Fernet para cifrado:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Cookies

- En desarrollo, `https_only` queda desactivado para permitir HTTP local.
- En produccion, la cookie de sesion se marca como segura.
- `SameSite` por defecto es `lax`.
- `SESSION_MAX_AGE` se mantiene explicito.

## Hosts y CORS

- Hosts locales por defecto: `localhost`, `127.0.0.1`, `testserver`.
- Origenes CORS locales por defecto: puertos 8000 y 8001 en `localhost` y `127.0.0.1`.
- En produccion, ambos valores deben venir de entorno si se necesitan.

## Secretos

- Los formularios muestran campos vacios o enmascarados.
- Los secretos nunca deben aparecer completos en `repr`, logs o respuestas.
- El valor real permanece en la base o en entorno, no en HTML ni JSON de diagnostico.

## Errores esperados de arranque

- `APP_ENV must be development, test or production`
- `Unsafe SECRET_KEY for production`
- `ENCRYPTION_KEY is required in production`
- `DEBUG cannot be enabled in production`
- `ENABLE_DEMO_BOOTSTRAP cannot be enabled in production`
- `ALLOWED_HOSTS must be explicit in production`
- `CORS wildcard is not allowed with credentials`
- `DATABASE_URL cannot use sqlite in production`
- `MASTER_DATABASE_URL cannot use sqlite in production`

## Ejemplo seguro

```env
APP_ENV=production
SECRET_KEY=pon-una-clave-larga-y-aleatoria
ENCRYPTION_KEY=pon-una-clave-fernet-valida
ENABLE_DEMO_BOOTSTRAP=false
DEBUG=false
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_SAMESITE=lax
SESSION_MAX_AGE=604800
ALLOWED_HOSTS=app.ejemplo.com
CORS_ALLOWED_ORIGINS=https://app.ejemplo.com
DATABASE_URL=postgresql+psycopg://user:password@host:5432/anchi
MASTER_DATABASE_URL=postgresql+psycopg://user:password@host:5432/anchi_master
```
