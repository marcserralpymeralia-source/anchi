# Checklist de discovery de cliente Odoo

Este checklist debe completarse antes de habilitar cualquier transporte Odoo real. El PoC de BLOQUE 6 usa fixtures y no necesita ni acepta una API key real.

## Plataforma y contrato

- [ ] Versión exacta de Odoo; confirmar si es 19 o superior.
- [ ] Odoo Online, Odoo.sh u on-premise.
- [ ] Plan contratado; confirmar que el acceso a External API está disponible.
- [ ] URL base y, si aplica, proxy/reverse proxy.
- [ ] Nombre de la base de datos para `X-Odoo-Database`, si el deployment lo necesita.
- [ ] Acceso a la documentación dinámica de la base: `/doc`.
- [ ] Exportar o revisar los modelos, métodos y campos disponibles en `/doc`.

## Módulos y seguridad

- [ ] Sales instalado y operativo.
- [ ] Inventory instalado si se necesita `inventory_source`.
- [ ] Pricelists y reglas de precio confirmadas.
- [ ] Usuario bot dedicado, no administrador global.
- [ ] Access rights, record rules y field access probados para cada operación.
- [ ] API key creada con descripción, expiración y procedimiento de rotación.
- [ ] Referencia del secreto registrada en SecretProvider; nunca pegar la key en `config_json`.

## Multi-company y datos

- [ ] Compañías Odoo accesibles por el usuario bot.
- [ ] Política de compañía/contexto (`allowed_company_ids`, idioma y moneda).
- [ ] Clientes en `res.partner`; confirmar campos y reglas de identificación.
- [ ] Productos en `product.product`; confirmar relación con `product.template`.
- [ ] Variantes, atributos y códigos externos que deben conservarse.
- [ ] Almacenes, ubicaciones y reglas de stock.
- [ ] Tarifas, descuentos, impuestos y moneda de venta.
- [ ] Campos custom de cliente/producto.
- [ ] Campos custom de `sale.order` y `sale.order.line`.

## Operativa de pedidos

- [ ] Confirmar si ANCHI crea un presupuesto en draft o confirma el pedido.
- [ ] Verificar `sale.order.create` y la forma de crear líneas en `/doc`.
- [ ] Verificar si `action_confirm` es necesario y quién lo ejecuta.
- [ ] Acordar campo o módulo para `client_order_ref`/idempotency key.
- [ ] Probar lookup después de un timeout para evitar duplicados.
- [ ] Acordar qué errores son revisión manual y cuáles permiten retry.

## Disponibilidad comercial

- [ ] Confirmar que el plan no es One App Free/Standard si se requiere External API.
- [ ] Confirmar límites, expiración/rotación de API keys y contacto de soporte.
- [ ] Registrar módulos custom que puedan cambiar el contrato estándar.
