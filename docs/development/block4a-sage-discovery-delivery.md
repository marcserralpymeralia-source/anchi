# Entrega de fase — BLOQUE 4A

## 1. Objetivo

Descubrir y seleccionar la primera variante Sage para ANCHI sin implementar runtime ni efectuar llamadas reales.

## 2. Alcance ejecutado

- Comparación de Sage 200 España, Sage Active ES/PT, Sage 200 UKI, Sage 50 España, Sage X3 y Sage 300.
- Identificación de APIs, prerequisitos de licencia, autenticación, despliegue y gaps documentales.
- Discovery GitHub/open source con clasificación de reutilización.
- Recomendación explícita de `Sage200ESConnector` condicionada a tenant y contrato.
- Checklist de cliente y plan de Block4B.

## 3. Alcance no ejecutado

- No se ha creado `SageConnector` ni ningún adaptador runtime.
- No se han añadido dependencias.
- No se han usado credenciales.
- No se han realizado llamadas HTTP a Sage.
- No se han usado datos de producción.
- No se ha modificado la matriz de reuso global porque la conclusión de BLOQUE 0.5 no cambia.

## 4. Diagnóstico previo

Sage 200 España ofrece una superficie de Soluciones Conectadas condicionada por licencia, edición, versión y administración. El contrato completo de entidades y operaciones no está publicado en las páginas públicas consultadas. Sage Active tiene una API pública más demostrable, pero no se ha establecido que sea la variante predominante entre clientes GEMAVI.

## 5. Cambios realizados

Se añadieron documentos de investigación y se amplió el registro open source con proyectos Sage relevantes. No se cambió código de aplicación.

## 6. Archivos modificados

| Archivo | Motivo | Tipo de cambio |
|---|---|---|
| `docs/research/open-source-registry.md` | Registrar proyectos Sage revisados | Documentación |

## 7. Archivos creados

| Archivo | Finalidad |
|---|---|
| `docs/research/sage-connector-matrix.md` | Matriz comparativa de variantes |
| `docs/research/sage-api-analysis.md` | Análisis A-L de contrato, riesgos y selección |
| `docs/integrations/sage-client-discovery-checklist.md` | Checklist para cliente/partner |
| `docs/development/block4b-sage-poc-plan.md` | Plan de POC sin implementar |
| `docs/development/block4a-sage-discovery-delivery.md` | Registro de entrega conforme a plantilla |

## 8. Decisiones técnicas

| Decisión | Motivo | Alternativas descartadas |
|---|---|---|
| Primer candidato `Sage200ESConnector` | Evidencia directa de API en España y encaje de mercado | `SageConnector` genérico, Sage 200 UKI, Sage 300, Sage X3 |
| Sage Active queda como alternativa | API pública más completa, pero representatividad de cliente no confirmada | Mezclar sus DTOs/auth con Sage 200 |
| UNKNOWN explícitos | Evitar convertir documentación incompleta en contrato operativo | Inferir España desde UKI o Active |
| POC con outbox y reconciliación | No hay idempotency key pública confirmada para Sage 200 ES | Reintentos ciegos |

## 9. Validaciones ejecutadas

| Comando | Resultado |
|---|---|
| `rtk git status --short --branch` | Rama correcta `feat/modular-order-hub`; estado limpio antes de editar |
| `rtk rg --files ...` | `PROJECT.md` no está presente en este worktree; se aplicaron `AGENTS.md` y plantilla local |
| Revisión documental web | Fuentes Sage oficiales y repositorios públicos consultados; sin llamadas al runtime de ANCHI |
| `rtk git diff --check` | Sin errores de whitespace en el archivo tracked modificado |
| Revisión de archivos nuevos y estado Git | Solo documentación del BLOQUE 4A y registro open source; sin código runtime ni secretos detectados |

## 10. Tests añadidos o modificados

Ninguno. El bloque es documental y no modifica runtime.

## 11. Criterios de aceptación

| Criterio | Estado | Evidencia |
|---|---|---|
| Variantes estudiadas | Cumplido | `sage-connector-matrix.md` |
| APIs españolas y gaps | Cumplido | `sage-api-analysis.md` |
| Identidad cliente/producto | Cumplido | Sección F y checklist |
| Idempotencia/reconciliación | Cumplido | Sección G y plan B4B |
| Auth/despliegue | Cumplido | Sección H y checklist |
| GitHub/open source | Cumplido | Registro y sección I |
| Primer connector recomendado | Cumplido | `Sage200ESConnector`, condicionado |
| Checklist cliente | Cumplido | Documento de integraciones |
| Plan Block4B | Cumplido | Documento de desarrollo |
| Runtime sin cambios | Cumplido | Solo documentos dentro del alcance |

## 12. Riesgos y observaciones pendientes

- La variante real de los clientes GEMAVI puede ser Sage Active y cambiar la selección.
- El contrato detallado de Sage 200 España requiere acceso autorizado a portal/tenant/partner.
- No hay precio público universal del API/connector; debe confirmarlo Sage/partner.
- Idempotencia y búsqueda por referencia externa siguen sin confirmarse.
- Sage 50 España queda como discovery separado.

## 13. Desviaciones respecto al alcance inicial

Se añade este registro de entrega para cumplir la plantilla del repositorio. No se modifica código ni se abre una fase de implementación.

## 14. Estado final de Git

Debe verificarse al finalizar: solo los cuatro documentos requeridos, este registro y la ampliación del registro open source.

## 15. Recomendación para la siguiente fase

Ejecutar Block4B únicamente con un cliente piloto y tenant no productivo. Si el cliente es Sage Active, cambiar explícitamente el producto objetivo antes de implementar cualquier adaptador.
