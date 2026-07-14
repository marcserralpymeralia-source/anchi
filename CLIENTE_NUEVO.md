# Replicar la app para un nuevo cliente

Esta base esta preparada para reutilizarse sin tocar codigo. La personalizacion de cada cliente debe hacerse con variables de entorno, importaciones y la pantalla de Configuracion.

## 1. Crear configuracion del cliente

```bash
cd backend
cp .env.example .env
```

Edita `.env`:

- `APP_NAME`: nombre tecnico visible en titulo/base.
- `APP_SLUG`: identificador unico del cliente, sin espacios.
- `APP_ENV`: `development` para local, `production` para entrega real.
- `DATABASE_URL`: base de datos propia del cliente.
- `MASTER_DATABASE_URL`: base master compartida o dedicada.
- `DEFAULT_COMPANY_NAME`: razon social o nombre comercial inicial.
- `DEFAULT_ADMIN_EMAIL` y `DEFAULT_ADMIN_PASSWORD`: credenciales iniciales.
- `SECRET_KEY`: clave de sesion unica por instalacion.
- `ENCRYPTION_KEY`: clave Fernet unica por instalacion.
- `BRANDING_*`: identidad inicial.

Mantener `ENABLE_DEMO_BOOTSTRAP=false` para entregas reales.

## 2. Arrancar instalacion limpia

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Al primer arranque se crean:

- Empresa inicial.
- Roles base.
- Usuario administrador inicial.
- Prompts base del agente.
- Configuracion editable.

No se crean clientes, productos ni pedidos demo salvo que `ENABLE_DEMO_BOOTSTRAP=true`.

## 3. Personalizar desde la app

Entrar con el admin inicial y revisar:

- Configuracion > General.
- Configuracion > Identidad corporativa.
- Configuracion > Correo.
- Configuracion > Agente IA / LLM.
- Configuracion > Scoring.
- Configuracion > FTP/SFTP y Exportacion.

Despues importar:

- Clientes desde CSV/Excel.
- Productos desde CSV/Excel.
- Alias, dominios, referencias alternativas y equivalencias.

## 4. Checklist antes de entregar

- Cambiar password del admin inicial.
- Confirmar que `.env` no se sube al repositorio.
- Confirmar que la base de datos del cliente es nueva.
- Configurar correo real y probar lectura.
- Configurar LLM/API key y probar el agente.
- Configurar exportacion/FTP si aplica.
- Revisar identidad visual en login, sidebar y modal de revision.
- Importar datos maestros reales.
- Ejecutar una prueba con un pedido PDF real del cliente.

## 5. Donde no personalizar

Evita modificar codigo para cambios de cliente. Estos aspectos ya son configurables:

- Nombre de empresa y app.
- Logo, favicon, claims, colores y microcopy.
- Correo IMAP/SMTP.
- Proveedor y modelos LLM.
- Prompts.
- Scoring.
- FTP/SFTP.
- Exportacion.
- Usuarios y permisos.
- Clientes, productos y equivalencias.
