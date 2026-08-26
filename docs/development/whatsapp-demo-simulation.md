# Simulación local de WhatsApp

El script `backend/scripts/simulate_whatsapp_demo.py` genera eventos sintéticos con el formato de los webhooks de Meta y los pasa por el parser y la persistencia reales de Anchi.

Incluye:

- mensaje entrante con texto de pedido;
- estado de entrega;
- eco de un mensaje enviado desde WhatsApp Business App;
- sincronización de historial con mensajes entrantes y salientes;
- sincronización de contacto;
- `account_update` de reconexión.

El simulador no llama a Meta, no usa tokens y está bloqueado en Vercel y producción.

Desde la raíz del proyecto y con el servidor local detenido o en ejecución:

```powershell
$env:APP_ENV = "development"
$env:PYTHONPATH = "$PWD\backend"
python -m scripts.simulate_whatsapp_demo --company-slug anchi-demo
```

Después de ejecutarlo, abre `http://127.0.0.1:8001/entries` y revisa la conversación generada. Cada ejecución usa un identificador distinto para no reutilizar mensajes.

Para encolar también el procesamiento del pedido en el worker local:

```powershell
python -m scripts.simulate_whatsapp_demo --company-slug anchi-demo --enqueue
```

El flujo no modifica Vercel ni ninguna cuenta real de WhatsApp.
