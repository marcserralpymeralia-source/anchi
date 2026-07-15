# Fase 6D - Optimizacion del detalle de pedido

## Objetivo

Cerrar la ultima pieza grande de la experiencia de pedidos: el detalle de pedido. La meta era mantener la pantalla util para revision humana, pero sin el peso de cargar relaciones y catalogos completos.

## Diagnostico inicial

La vista de detalle era funcional, pero su coste crecia demasiado cuando el pedido arrastraba cliente, productos y contexto operativo. El problema no estaba en la UX visible, sino en la forma de cargar los datos.

## Alcance ejecutado

- Carga selectiva del pedido base.
- Snapshot estable para la cabecera de cliente.
- Reutilizacion de candidatos de cliente y producto.
- Render ligero del template de detalle.
- Cobertura de rendimiento para que no vuelva a crecer de forma silenciosa.

## Fuera de alcance

- No se rediseño la navegacion global.
- No se cambio el modelo de dominio de pedidos.
- No se altero la logica funcional de scoring ni de revision.

## Cambios principales

1. Se elimino la dependencia de carga completa de relaciones en el detalle.
2. Se dejo la cabecera apoyada en snapshot en vez de ORM perezoso.
3. Se consolidaron helpers compartidos para evitar consultas repetidas.
4. Se añadio una prueba especifica de presupuesto y regresion.
5. Se dejo documentado el presupuesto final y su comparativa.

## Validaciones ejecutadas

- `APP_ENV=test ./.venv/bin/python -m unittest tests.test_orders_detail_optimization`
- `APP_ENV=test ./.venv/bin/python -m unittest discover -s tests`
- `APP_ENV=development ./.venv/bin/python -m compileall app`
- `APP_ENV=development ./.venv/bin/python -c "from app.main import app; print(app.title)"`
- Benchmark small, medium y large

## Criterios de aceptacion

- El detalle responde sin cargar el catalogo completo.
- El presupuesto de consultas queda dentro de limite.
- La pantalla sigue mostrando las piezas operativas necesarias.
- La validacion automatica protege la regresion.

## Riesgos residuales

- El tiempo total en escenario large sigue dominado por SQL, aunque con menos volumen de datos visibles.
- La ruta de control `/history` sigue siendo mas costosa que otras y queda como siguiente area natural de observacion.

## Estado final

- Commit funcional: `337e02f Optimize order detail view`
- Cierre documental: esta fase

