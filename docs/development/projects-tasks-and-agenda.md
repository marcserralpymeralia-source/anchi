# Proyectos, tareas, agenda e imputacion de horas

## Alcance

- Crear un MVP operativo para gestionar proyectos, tareas, agenda e imputacion de horas dentro del tenant.
- Mantener la vista por defecto centrada en tareas asignadas al usuario actual.
- Permitir crear tareas de forma manual y tambien desde una entrada recibida.
- Mantener separadas las horas estimadas, las horas planificadas y las horas realmente imputadas.

## Decisiones tecnicas

- Las entidades viven en la base tenant y no en master.
- La agenda se apoya en `task_schedules`, no en `due_date`.
- La imputacion real se guarda en `time_entries`.
- El temporizador activo se modela con `active_timers` y se convierte a `time_entries` al detenerlo.
- La creacion desde una entrada recibida usa `source_type` y `source_reference` para evitar duplicados.
- El acceso operativo se expone en `/agenda`, `/projects` y `/tasks`.

## Flujo operativo

1. El usuario entra en la agenda o en tareas.
2. Crea o edita un proyecto.
3. Crea una tarea manualmente o desde un correo/entrada.
4. Programa bloques de agenda.
5. Registra horas manuales o con temporizador.
6. Revisa el historial y el balance estimado vs real.

## Riesgos pendientes

- La version MVP no incluye dependencia entre tareas ni Gantt.
- La programacion es simple y no resuelve conflictos de agenda.
- El temporizador es utilitario y no gestiona sesiones largas distribuidas.
- El formulario de alta rapida sigue siendo intencionalmente sencillo.

## Validacion

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_projects_tasks_agenda`
- `APP_ENV=development ./.venv/bin/python -m compileall app scripts tests`

