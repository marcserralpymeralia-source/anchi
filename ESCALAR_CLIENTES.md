# Como escalar la app por cliente

La forma mas rapida y segura es trabajar con **un codigo base comun** y **un entorno aislado por cliente**.

## Modelo recomendado

Usa este esquema:

- 1 repositorio Git con el codigo de la app.
- 1 `.env` por cliente.
- 1 base de datos por cliente.
- 1 almacenamiento de adjuntos por cliente.
- 1 dominio/subdominio por cliente.

Ejemplos:

- `cliente-a.tu-dominio.com`
- `cliente-b.tu-dominio.com`
- `cliente-c.tu-dominio.com`

Todos usan el mismo codigo, pero cada uno tiene su configuracion, base de datos, correo, LLM, branding y exportacion.

## Flujo rapido de alta

1. Crear configuracion:

```bash
python scripts/new_client.py \
  --name "Cliente Nuevo SL" \
  --slug cliente_nuevo \
  --admin-email admin@cliente.com \
  --app-name "Cliente Nuevo Agent" \
  --primary-claim "Gestion inteligente de pedidos"
```

2. Activar ese entorno localmente:

```bash
cp clients/cliente_nuevo.env backend/.env
cd backend
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

3. Entrar con el admin inicial del `.env`.

4. Configurar en la app:

- Identidad corporativa.
- Correo IMAP/SMTP.
- Agente IA / LLM.
- Scoring.
- Exportacion / FTP.
- Usuarios.

5. Importar datos reales:

- Clientes.
- Productos.
- Alias.
- Dominios.
- Codigos alternativos.

6. Probar con pedidos reales del cliente.

## Que guardar en Git

Guardar:

- Codigo.
- Migraciones.
- Plantillas.
- `.env.example`.
- Scripts.
- Documentacion.

No guardar:

- `backend/.env`.
- `clients/*.env`.
- Bases de datos.
- Adjuntos PDF.
- Exportaciones.
- Claves/API keys.
- Passwords.

## Cuando tengas muchos clientes

Fase 1: despliegue simple por cliente.

- Copia del mismo codigo.
- `.env` distinto.
- DB distinta.
- Proceso/servicio distinto.

Fase 2: Docker.

- Imagen unica de la app.
- Un contenedor por cliente.
- Volumen por cliente.
- Variables de entorno por cliente.

Fase 3: panel interno de provisionamiento.

- Formulario: nombre cliente, dominio, admin, branding.
- Crea `.env` o secretos.
- Crea base de datos.
- Ejecuta migraciones.
- Levanta servicio.

## Checklist comercial/operativo

Antes de entregar:

- Crear entorno aislado.
- Configurar dominio/subdominio.
- Cambiar password inicial.
- Cargar logo y colores.
- Configurar correo.
- Configurar LLM.
- Importar clientes y productos.
- Probar 3 pedidos reales.
- Validar exportacion a gestion.
- Crear usuario operativo del cliente.
- Desactivar o cambiar admin inicial.

## Decision importante

No personalices codigo para cada cliente salvo que sea funcionalidad reutilizable. Si un cliente pide algo especifico, intenta convertirlo en:

- ajuste en Configuracion,
- campo configurable,
- plantilla,
- regla,
- prompt,
- opcion de exportacion,
- o permiso.

Asi cada mejora vuelve al producto base y el siguiente cliente se beneficia.
