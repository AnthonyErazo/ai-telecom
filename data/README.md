# `data/` — deliberadamente vacío en el repositorio

Este directorio existe para que el proyecto funcione, pero **su contenido no se versiona**.
`.gitignore` ignora `data/*` con una única excepción: este archivo.

## Por qué

**[CONFIRMADO-OFICIAL]** Las BASES de la Hackathon AI Telecom 2026 establecen que
*"la información, datos y archivos proporcionados por Movistar son confidenciales y no
divulgables"*, con una obligación de confidencialidad vigente durante **10 años** posteriores
al fin de la Hackathon.

En consecuencia:

1. **Ningún dato entregado por Movistar/Integratel entra jamás en este repositorio**, ni
   siquiera en fragmentos, ni en tests, ni en ejemplos de documentación, ni en capturas.
2. Todo lo que el proyecto usa para funcionar y demostrarse es **sintético**, generado por
   nosotros con `packages/datagen` a partir de una semilla pública (`DEMO_SEED=20260804`).
3. El repositorio no puede volverse público con datos reales dentro. Manteniendo `data/`
   vacío, el repositorio es seguro por construcción y no depende de la disciplina de nadie.

**[CONFIRMADO-OFICIAL]** Además, la ficha del Desafío 1 indica que los datos que se
compartirán son *"base sintética/ficticia (Dummy Data) ... sin PII real (sin DNI ni teléfono)"*.
Aun así aplicamos la regla anterior: **el dato de Movistar, aunque sea dummy, no se commitea.**

## Qué se espera encontrar aquí en tiempo de ejecución

```
data/
├─ README.md                  <- lo único versionado
├─ sintetico/                 <- salida de `make seed` / `python -m packages.datagen.generar`
│  ├─ bills/{cuenta_id}.json  <- recibo actual + 5 previos, estilo BrainyBill
│  ├─ ordenes.csv             <- historial de órdenes, estilo Amdocs (CRM)
│  ├─ catalogo.json           <- catálogo de conceptos seed
│  ├─ faq.json                <- preguntas frecuentes seed
│  └─ ground_truth.csv        <- gt_causa_delta, escrito en el mismo acto de generar
├─ movistar/                  <- [VACÍO EN EL REPO] dataset real, solo en máquina local
└─ auditoria/                 <- JSONL append-only con cadena de hashes (tampoco se versiona)
```

## Cómo se regenera todo

```bash
python -m packages.datagen.generar --seed 20260804 --clientes 300 --salida data/sintetico/
# o, con el entorno levantado:
make seed
```

La generación es **determinística**: la misma semilla produce byte a byte el mismo dataset,
por lo que borrar `data/` nunca pierde información — solo hay que volver a generarla.

## Si algún día llega el dataset real de Movistar

Se coloca **fuera del control de versiones** (por ejemplo en `data/movistar/`, ya ignorado) y
se adapta **un solo archivo**: `packages/datagen/mapping/movistar_map.py`, que es el ACL
declarativo (`COLUMN_MAP`, `CONCEPTO_MAP`, `validate()`) entre el esquema real y el modelo
canónico del proyecto. Ningún otro módulo cambia.
