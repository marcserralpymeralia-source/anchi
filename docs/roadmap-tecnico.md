# Roadmap técnico recomendado

## Prioridades

1. Mantener estable la multitenencia.
2. Seguir aligerando home, workbench y settings.
3. Cerrar el runtime normal sin puentes legacy.
4. Mejorar jobs, observabilidad y reintentos.
5. Consolidar learning y backfill IMAP.
6. Solo después ampliar canales o inventar nuevos módulos de negocio.

## Fases

### Fase 0. Estabilización

**Objetivo:** dejar la base técnica cerrada y confiable.

**Incluye:**
- login/master/tenant sin ambigüedad;
- revisión de rutas legacy;
- control de errores del arranque;
- estado de jobs y health;
- eliminación de cargas innecesarias en home.

**Riesgo:** medio.

**Criterio de aceptación:** la app arranca, autentica y resuelve tenant de forma consistente.

### Fase 1. Normalización de operación

**Objetivo:** que pedidos, correos, clientes y productos vivan en una experiencia única y clara.

**Incluye:**
- bandeja de pedidos más limpia;
- revisión de pedido más rápida;
- maestros de clientes y productos más sencillos;
- importación más usable;
- histórico más accesible.

**Riesgo:** medio.

**Criterio de aceptación:** un operador puede revisar y corregir pedidos con menos fricción.

### Fase 2. Observabilidad y rendimiento

**Objetivo:** que el sistema pesado sea visible y controlable.

**Incluye:**
- jobs con detalle, retry y error history;
- carga diferida del detalle operativo;
- backfill IMAP controlado por fecha;
- métricas y diagnóstico admin más útiles;
- menos trabajo síncrono en requests.

**Riesgo:** medio-alto.

**Criterio de aceptación:** los procesos largos se ven, se reintentan y no bloquean la UI.

### Fase 3. Aprendizaje y conocimiento

**Objetivo:** convertir correcciones humanas en conocimiento útil.

**Incluye:**
- aliases aprendidos;
- RAG de casos resueltos;
- consolidación de manual corrections;
- mejora de prompts y versiones.

**Riesgo:** medio.

**Criterio de aceptación:** el sistema aprende sin sustituir la validación determinista.

### Fase 4. Nuevos canales

**Objetivo:** añadir WhatsApp, voz y redes sobre la misma base.

**Incluye:**
- activación por tenant;
- credenciales y settings por canal;
- adaptación de ingestión;
- continuidad del pipeline común.

**Riesgo:** alto.

**Criterio de aceptación:** cada canal nuevo usa el mismo pipeline de entrada.

### Fase 5. Expansión funcional futura

**Objetivo:** si el producto lo requiere, crear un módulo real de proyectos/tareas/calendario/imputación.

**Incluye:**
- modelo de tareas;
- calendario;
- imputación;
- capacidad diaria;
- asignaciones.

**Riesgo:** alto si se mezcla con la base actual sin un diseño nuevo.

**Criterio de aceptación:** el módulo nace con su propio dominio y no contamina pedidos.

## Dependencias

- La fase 0 depende de que master/tenant sigan sólidos.
- La fase 1 depende de que pedidos/correo estén estables.
- La fase 2 depende de jobs y worker correctamente aislados.
- La fase 3 depende de mantener el pipeline común.
- La fase 4 depende de la abstracción de canales.
- La fase 5 depende de que se acepte construir un dominio nuevo.

## Orden de ejecución

1. Cerrar lo que sigue cargando de más en home y settings.
2. Fortalecer jobs y observabilidad.
3. Terminar de limpiar legacy/puentes.
4. Consolidar IMAP/backfill y learning.
5. Expandir canales.
6. Solo después introducir agenda/proyectos/tareas si sigue siendo objetivo.

## Riesgos principales

- secretos o defaults demo en despliegue real;
- mezcla accidental de master y tenant;
- crecimiento de la carga síncrona;
- exceso de peso en plantillas grandes;
- deuda técnica al seguir añadiendo canales sin cerrar la base.

## Criterios de aceptación globales

- cada compañía opera en su tenant;
- los flujos pesados están desacoplados;
- la home es rápida;
- jobs y alertas son operables;
- learning añade valor sin romper determinismo;
- nuevos canales se enchufan sin reescribir el núcleo.
