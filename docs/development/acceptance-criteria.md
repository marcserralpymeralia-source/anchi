# Criterios de aceptacion

## Globales

- Mantener el aislamiento master/tenant.
- No introducir secretos en codigo.
- No modificar funcionalidades fuera del alcance.
- No añadir dependencias sin justificacion.
- No duplicar logica ya existente.
- Mantener compatibilidad con el flujo principal.

## Seguridad

- No exponer credenciales ni tokens.
- No usar datos de produccion.
- No ejecutar acciones destructivas.

## Tests

- Ejecutar los tests relacionados antes de cerrar una fase.
- Ejecutar validacion sintactica.
- Documentar cualquier fallo que no se corrija en ese momento.

## Compatibilidad

- No romper el arranque local.
- No cambiar rutas ni endpoints salvo que el objetivo lo requiera.
- No alterar el modelo de datos salvo instruccion explicita.

## Documentacion

- Enumerar archivos modificados.
- Enumerar archivos creados.
- Registrar decisiones tecnicas.
- Registrar riesgos pendientes.

## Control de alcance

- Solo modificar archivos necesarios para el objetivo concreto.
- Documentar cualquier problema detectado fuera de alcance.

## Condicion de detencion

- Detenerse al terminar el alcance solicitado, sin empezar la fase siguiente.

