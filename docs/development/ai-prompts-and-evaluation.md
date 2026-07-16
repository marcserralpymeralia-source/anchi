# Prompts de IA y evaluacion reproducible

## Objetivo

Registrar cada ejecucion de prompt, validar su salida estructurada y disponer de una utilidad simple para comparar salidas esperadas y reales sin depender de llamadas a IA en tests.

## Criterios operativos

- Cada ejecucion queda guardada en `prompt_executions`.
- La clasificacion y la extraccion solo aceptan JSON valido.
- La salida invalida se marca como tal antes de entrar al pipeline.
- La utilidad de evaluacion trabaja con fixtures locales.
- Los tests usan salidas sinteticas o fixtures, nunca IA real.
