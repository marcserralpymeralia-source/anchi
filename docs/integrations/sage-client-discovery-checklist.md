# Checklist de discovery con cliente Sage

Objetivo: determinar si un cliente puede participar en el POC de ANCHI sin pedir secretos en claro ni ejecutar cambios en producción.

## 0. Regla de seguridad

- No solicitar ni pegar contraseñas, client secrets, tokens, cookies, exports de producción o capturas con credenciales.
- No pedir acceso directo a la base de datos.
- Usar tenant demo, empresa de prueba o datos sintéticos.
- Entregar secretos únicamente mediante el canal seguro acordado, después de que los gates documentales estén aprobados.
- Revocar credenciales de prueba al cerrar el POC.

## 1. Identificación del producto

- [ ] ¿Es Sage 200 España, Sage Active, Sage 50 España, Sage 200 UKI, Sage X3 u otro producto?
- [ ] ¿Qué país y legislación usa la empresa?
- [ ] ¿Qué edición exacta tiene?
- [ ] ¿Es local/desktop, online, cloud o Azure alojado?
- [ ] ¿Qué versión/build exacto aparece en la instalación o portal?
- [ ] ¿Cuántas empresas/compañías están dentro del alcance?
- [ ] ¿El cliente necesita una sola empresa o varias?
- [ ] ¿Existe un partner Sage responsable que pueda confirmar el contrato API?

**Gate:** si la variante no es inequívoca, no seleccionar `Sage200ESConnector`.

## 2. Licencia y activación

Para Sage 200 España:

- [ ] ¿Tiene licencia de Sage Soluciones Conectadas?
- [ ] ¿La edición es Sage 200 Advanced?
- [ ] ¿La versión cumple el mínimo documentado por Sage?
- [ ] ¿Un administrador puede activar la API y crear el alias?
- [ ] ¿Existe Sage ID habilitado para el administrador?
- [ ] ¿Puede invitarse una identidad técnica o de integración?
- [ ] ¿Puede asignarse un usuario de aplicación con solo las empresas necesarias?
- [ ] ¿El partner confirma por escrito que el uso de la API cubre el caso de ANCHI?

**Evidencia aceptable:** confirmación del portal o partner, no una suposición basada en la interfaz.

## 3. Acceso no secreto

Solicitar primero únicamente:

- [ ] nombre de producto y versión;
- [ ] modalidad de alojamiento;
- [ ] nombre lógico del entorno (`demo`, `sandbox`, `test`), nunca contraseña;
- [ ] documentación/portal de desarrolladores aplicable;
- [ ] lista de entidades y operaciones permitidas por licencia;
- [ ] requisitos de allowlist, VPN, proxy o certificado;
- [ ] expiración y rotación esperada de credenciales;
- [ ] límites conocidos de frecuencia o tamaño;
- [ ] soporte de webhooks/eventos frente a polling.

No pedir todavía valores de `client_secret`, token, cookie, contraseña o certificado privado.

## 4. Modelo de clientes

En un tenant no productivo, validar de forma controlada:

- [ ] clave estable del cliente;
- [ ] nombre legal y nombre comercial;
- [ ] email/teléfono si aplica;
- [ ] dirección y dirección de entrega;
- [ ] moneda e impuestos;
- [ ] estado activo/bloqueado;
- [ ] varias empresas con el mismo código;
- [ ] paginación y filtro por actualización;
- [ ] permisos mínimos necesarios.

Preguntas de mapping:

- ¿La clave que ve el usuario es la misma que devuelve la API?
- ¿Puede cambiar el código de cliente?
- ¿Existe una referencia externa dedicada para ANCHI?
- ¿Qué debe ocurrir si el cliente no existe: bloquear, crear o revisión humana?

## 5. Modelo de productos y catálogo

- [ ] código/SKU estable;
- [ ] descripción y descripción corta;
- [ ] unidad de venta y conversiones;
- [ ] impuestos;
- [ ] moneda;
- [ ] precio base;
- [ ] tarifas por cliente/segmento;
- [ ] descuentos y tramos;
- [ ] stock total y por almacén;
- [ ] producto activo/no vendible;
- [ ] imágenes o referencias de catálogo, si el negocio las necesita;
- [ ] actualización incremental (`updated_at`, cursor o equivalente).

No asumir que “producto” equivale a “artículo vendible” ni que “stock” equivale a disponibilidad prometible.

## 6. Pedido mínimo para el POC

Definir un caso de prueba sintético:

- [ ] un cliente existente;
- [ ] una línea de producto existente;
- [ ] cantidad decimal si el negocio la necesita;
- [ ] precio/tarifa conocida;
- [ ] impuesto y moneda;
- [ ] dirección de entrega;
- [ ] referencia externa de ANCHI;
- [ ] nota no sensible;
- [ ] estado inicial esperado;
- [ ] respuesta que contiene ID/número remoto.

Validar por separado:

- [ ] validación sin efectos laterales;
- [ ] creación de pedido;
- [ ] lectura por ID remoto;
- [ ] búsqueda por referencia externa;
- [ ] estado tras creación;
- [ ] repetición exacta;
- [ ] timeout con commit remoto desconocido;
- [ ] error de negocio no retryable;
- [ ] retryable temporal;
- [ ] cancelación o corrección, si aplica.

## 7. Idempotencia y reconciliación

Preguntar al cliente/partner y probar en sandbox:

- [ ] ¿hay header o campo idempotente?
- [ ] ¿qué referencia externa conserva Sage?
- [ ] ¿se puede filtrar por esa referencia?
- [ ] ¿qué ventana temporal y paginación tiene la búsqueda?
- [ ] ¿qué sucede al reintentar tras un 504 o desconexión?
- [ ] ¿hay eventos de cambio de estado?
- [ ] ¿qué identificador es definitivo: ID, número operativo o ambos?
- [ ] ¿hay operaciones que generen documentos adicionales asíncronamente?

**Gate:** si no existe una consulta segura posterior al timeout, Block4B no autoriza efectos reales; solo fixture/mock.

## 8. Autenticación y despliegue

- [ ] tipo de auth y grant;
- [ ] scopes/roles mínimos;
- [ ] duración de access token;
- [ ] refresh/rotación/revocación;
- [ ] certificados requeridos;
- [ ] IP allowlist o VPN;
- [ ] proxy corporativo;
- [ ] DNS/TLS desde el entorno de ANCHI;
- [ ] límites por minuto y por día;
- [ ] ventana de mantenimiento;
- [ ] contacto de soporte y escalado.

Para instalaciones locales, decidir explícitamente entre acceso saliente desde ANCHI, VPN, allowlist o agente instalado por el cliente. No abrir puertos entrantes como solución implícita.

## 9. Datos y privacidad

- [ ] confirmar qué campos personales devuelve la API;
- [ ] minimizar snapshots y logs;
- [ ] enmascarar email, teléfono y dirección en trazas;
- [ ] definir retención de payload original;
- [ ] acordar quién puede ver errores y pedidos;
- [ ] usar datos sintéticos en capturas y fixtures;
- [ ] documentar base legal/encargo según el contrato del servicio.

## 10. Paquete de salida del discovery

El cliente debe devolver, sin secretos:

1. producto, edición, país y versión;
2. confirmación de licencia/API;
3. lista de empresas de prueba;
4. operaciones autorizadas;
5. documentación o portal aplicable;
6. contacto técnico/partner;
7. topología de red y requisitos de allowlist;
8. muestra sintética de clientes/productos/pedido;
9. política de errores, límites e idempotencia conocida;
10. aprobación para consumir solo el tenant de prueba.

## Criterio de go/no-go

**GO documental** si producto, licencia, tenant de prueba, usuario técnico, alcance de empresas y contrato API están confirmados.

**GO técnico** solo si el POC demuestra lectura de cliente/producto, validación de pedido, creación controlada, lectura posterior y reconciliación tras timeout.

**NO-GO** si se requiere producción para descubrir el contrato, no hay forma de revocar credenciales, no existe una identidad técnica limitada o no se puede determinar si un pedido remoto ya fue creado.

