# Instrucciones de trabajo

## Alcance

Modificar unicamente los archivos necesarios para el objetivo explicito del prompt.

Los problemas ajenos al alcance deben documentarse, no corregirse.

## Seguridad

No mostrar, registrar ni versionar secretos.

No utilizar datos de produccion.

No ejecutar acciones destructivas.

## Git

No descartar cambios locales.

No realizar commits ni push salvo instruccion explicita.

No cambiar de rama cuando existan cambios locales en riesgo.

## Validacion minima

Antes de finalizar:

1. Ejecutar validacion sintactica.
2. Ejecutar los tests relacionados.
3. Registrar comandos y resultados.
4. Revisar `git diff`.
5. Enumerar todos los archivos modificados.
6. Confirmar que no hay cambios fuera de alcance.

## Entrega

Usar la plantilla:

`docs/development/phase-delivery-template.md`
