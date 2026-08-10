# recibo-claro

**Asistente de explicación de recibos Movistar con cero invenciones financieras — verificadas en código, no prometidas en un prompt.**

Hackathon AI Telecom Challenge 2026 · Desafío 1: *Atención inteligente y explicación de recibos*
Integratel Perú S.A.A. (Movistar) + Facultad de Ingeniería de la Universidad de Lima.

```
make demo
```

---

## Convención de etiquetado

Este repositorio distingue siempre entre lo que dicen los documentos oficiales y lo que
proponemos nosotros. Nada de lo segundo se presenta como dato de Movistar.

| Etiqueta | Significado |
|---|---|
| `[CONFIRMADO-OFICIAL]` | Cita literal de las BASES o de la ficha del Desafío 1. Se transcribe entre comillas y se indica la sección. |
| `[SUPUESTO]` | Hipótesis de trabajo del equipo, necesaria para que el prototipo funcione. |
| `[PROPUESTA]` | Diseño nuestro. No es un requisito ni una afirmación sobre Movistar. |
| `[POR VALIDAR]` | Parámetro o dato que debe confirmar el equipo de facturación de Movistar antes de cualquier uso real. |

---

## 1. El problema, con las cifras oficiales

> **Fuente de todas las cifras de esta sección:** Ficha *«01. Desafío atención inteligente y
> explicación de recibos»*, Hackathon AI Telecom Challenge 2026, secciones «Contexto y
> problemática», «Indicadores» e «Información adicional». Todas son `[CONFIRMADO-OFICIAL]`.

- **Volumen.** *«Facturación B2C: más de 5 millones de recibos al mes, de los cuales
  aproximadamente el 40 % corresponde al clúster de "variación"»* — el monto cambió respecto
  del mes anterior. Ese 40 % es la población que puede necesitar una explicación.
- **Demanda ya existente.** *«En el Bot, facturación representa aproximadamente el 5 % de las
  atenciones. En la App, la explicación de recibo tiene aproximadamente 1 millón de
  transacciones.»*
- **El dato disponible pero mudo.** *«BrainyBill expone la información de la factura actual y
  de los cinco recibos previos, pero hoy NO explica el recibo de forma inteligente ni orientada
  al cliente.»* El problema no es de datos: es de explicación.
- **Por qué duele.** *«Muchos conceptos técnicos —prorrateos, reconexiones, notas de
  crédito/débito, ajustes por suspensión— responden a exigencias fiscales y regulatorias»*, y
  la falta de una explicación simple genera *«llamadas y contactos repetidos, derivaciones,
  reclamos, baja solución digital, menor NPS, mayores costos y pérdida de confianza»*, con
  *«alta propensión a la baja en clientes que no entienden su recibo o creen que se les cobra
  de más»*.
- **Qué se espera mover.** Reducción de llamadas al call center y de atención de asesor humano
  en WhatsApp **(~15 %)**; incremento del **NPS Transaccional FARECO (~10 %)** y del **NPS
  digital de la App Mi Recibo (~10 %)**; reducción de reclamos asociados a la explicación del
  recibo **(~5 %)**.
- **El listón técnico.** *«Tasa de Alucinación: Cero invenciones financieras **comprobables
  mediante logs de la terminal**.»* La ficha no pide "pocas alucinaciones": pide cero, y pide
  poder comprobarlo.
- **La carga.** *«Escalabilidad para picos de hasta 3 veces la volumetría normal.»*

### El caso que resume el desafío

Un cliente se pasa de un plan de **S/ 99.90** a uno de **S/ 79.90** y su recibo **sube
S/ 20.82**. No hay error de facturación: en **renta adelantada**, el recibo del mes contiene a
la vez la renta anticipada del mes que empieza (ya con el plan nuevo) y el ajuste retroactivo
de los días que se consumieron con el plan anterior; y la promoción atada al plan viejo murió
con el cambio. Es correcto, es explicable en dos frases, y hoy termina en una llamada al 104.

Ese cliente es `C-DEMO-01` en este repositorio, y `make demo` lo explica.

---

## 2. Qué es esto

Una **API canal-agnóstica** que, dado un `cuenta_id` y un periodo, responde *por qué* varió el
recibo, con qué acción seguir, y —cuando no puede sostener la respuesta— **deriva a un asesor
humano con el contexto ya cargado**.

No hay frontend: la solución se entrega como endpoints y bloques tipados que la App Mi
Movistar, el Bot Lucía o WhatsApp renderizan cada uno a su manera. Ver
[`ejemplos/curl.md`](ejemplos/curl.md) para el recorrido completo.

### Las cuatro decisiones que sostienen el "cero alucinaciones"

1. **El modelo generativo no calcula.** Recibe un `FactSet` ya conciliado y sellado con
   SHA-256. Solo aporta la prosa; **cada cifra del texto la inyecta el código** desde un entero
   del `FactSet`. → [`docs/ADR/003-el-llm-no-calcula.md`](docs/ADR/003-el-llm-no-calcula.md)
2. **Un verificador en código, no un juez LLM.** Extrae con expresiones regulares todas las
   cifras del texto final, las normaliza a tokens (`cent:2082`, `num:31`, `fecha:2026-07-12`) y
   las resta contra el conjunto permitido construido **solo** desde el `FactSet`. Una sola
   cifra sin anclar bloquea la respuesta y abre la derivación.
3. **Invariante de conciliación.** Si la suma de variaciones por concepto no reproduce la
   diferencia entre totales (tolerancia ±1 céntimo), **no se explica: se deriva**. Nunca hay
   "explicación aproximada".
4. **El recibo no se vectoriza.** Es consulta estructurada y aritmética exacta. Al índice
   vectorial solo van catálogo, FAQs y casuísticas — y pasan por un saneador que sustituye toda
   cifra por `«un monto»` antes de tocar el prompt.
   → [`docs/ADR/001-el-recibo-no-se-vectoriza.md`](docs/ADR/001-el-recibo-no-se-vectoriza.md)

### El resultado, medido

```
casos golden: 261   ·   proveedor: mock   ·   reglas: 1.0.0

1. PRECISIÓN DE RECUPERACIÓN
   (C) strict answer accuracy  ← TITULAR          100.00 %   261/261 respuestas exactas
   (A) field-level exact match · micro            100.00 %   1388/1388 campos
   (B) Recall@1 doc-level                         100.00 %   213 casos evaluables

2. TASA DE ALUCINACIÓN
   TA_respuesta  ← COMPROMETIDA EN 0                0.00 %   CUMPLE · 0/261 respuestas
   TA_asercion                                      0.00 %   0/4625 cifras auditadas

3. PRECISIÓN DEL HAND-OFF
   Recall_handoff  ← PRIMARIA                     100.00 %   VP 13 · FP 0 · VN 248 · FN 0
   Handoff_completeness (7 campos)                100.00 %   91/91 campos informados
```

**Leído con honestidad:** el ground truth y el sistema comparten autor, y `make eval` imprime
esa advertencia arriba y abajo de la tabla. Lo que estas cifras demuestran es que la mecánica
es correcta y reproducible. La única que se traslada tal cual a producción es
`TA_respuesta = 0`, porque el verificador **no** compara contra el ground truth: compara contra
el `FactSet` del propio cliente. Es una garantía estructural, no un resultado estadístico.

Suite de pruebas: **1.511 pasan, 299 se omiten** (las omitidas exigen `GEMINI_API_KEY` real o
PostgreSQL levantado).

**Y una métrica al 100 % puede convivir con un defecto de producto.** Lo encontramos leyendo el
texto generado, no mirando la tabla: en `C-DEMO-01` la explicación atribuye toda la subida al
cambio de plan cuando el cambio de plan, en realidad, **abarató** el recibo. Está documentado
con su corrección en [`docs/pendientes.md`](docs/pendientes.md) §1. Preferimos enseñarlo a que
lo encuentre el jurado.

---

## 3. Arranque en un comando

Hay dos rutas y las dos son de un comando. Elija según lo que tenga instalado:

| Si tiene… | Comando | Qué levanta |
|---|---|---|
| **Python 3.12** | `make dev` (o `python scripts/dev.py`) | API + consola `/ui`, **sin Docker y sin PostgreSQL** |
| **Docker** | `make demo` | PostgreSQL + pgvector, API y los dos mocks HTTP |

La ruta corta es la de Python: no exige base de datos ni red, genera el dataset si falta y
queda comprobable con `make probar`. Está detallada [más abajo](#sin-docker-y-sin-postgresql-la-ruta-mínima).

### Con Docker

**Requisito único: Docker.** No hace falta Python, ni `curl`, ni `jq` en el equipo.

```bash
git clone <repo> && cd recibo-claro
make demo
```

`make demo` encadena, en este orden:

| Paso | Qué hace | Por qué en ese orden |
|---|---|---|
| `build` | Construye la imagen multi-stage (`Dockerfile.api`) | La misma imagen sirve API y mocks |
| `migrate` | Aplica `db/migraciones/00{1,2,3}.sql` sobre PostgreSQL 16 + pgvector | Idempotente; con *advisory lock* y checksum por migración |
| `seed` | Genera el dataset sintético (300 clientes, 1 800 recibos) | Determinístico: misma semilla ⇒ mismos bytes |
| `indexar` | Vectoriza catálogo, FAQs y casuísticas | Antes de levantar la API, que cachea el corpus al arrancar |
| `up` | Levanta `db`, `api`, `mock-brainybill`, `mock-amdocs` | La API espera a que la BD esté *healthy* |
| `smoke` | Explica el recibo de `C-DEMO-01` | **Falla ruidosamente si `verificacion_numerica != PASS`** |

Salida esperada del último paso:

```
[3/4] POST /v1/explicar
      veredicto=PASS · totales=12 · ancladas=12 · no ancladas=0
[4/4] GET /v1/auditoria  (la prueba en el log)
      ╭─ RECIBO CLARO · trace tr-ec7383011318 ──────────────────╮
      │ AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 12 · NO ANCLADAS 0 │
      ╰─────────────────────────────────────────────────────────╯
        ✔ HECHOS      Δ +S/ 20.82 · 4 líneas · residual 0 c · invariante OK
        ✔ VERIFICA    PASS · 12 ancladas · 0 derivadas · 0 no ancladas · 12 citas
==============================================================================
SMOKE OK · explicación verificada, cero cifras sin anclar
==============================================================================
```

Si esa prueba devuelve `FAIL`, `make demo` **termina con código 1**: un despliegue que arranca
pero entrega una cifra sin respaldo está roto aunque responda `200`.

Después: <http://127.0.0.1:8000/docs> · `make logs` · `make down`.

> **Nota para Linux.** El contenedor corre como usuario sin privilegios con UID 1000 y escribe
> el dataset en el bind mount `./data`. Si su usuario tiene otro UID, `make seed` no podrá
> escribir: ajuste la propiedad con `sudo chown -R 1000:1000 data` o genere el dataset con el
> intérprete del equipo (`python -m packages.datagen.generar …`). En Docker Desktop (Windows y
> macOS) no aplica.

### Sin Docker y sin PostgreSQL (la ruta mínima)

**Requisito único: Python 3.12.** Ni Docker, ni base de datos, ni red.

```bash
make instalar          # pip install -e ".[dev]"     (una vez)
make dev               # genera el dataset si falta, levanta la API y abre /ui
make probar            # en otra terminal: recorrido completo con PASA/FALLA por paso
```

Quien prefiera no instalar el paquete tiene el mismo conjunto en
[`requirements.txt`](requirements.txt) y [`requirements-dev.txt`](requirements-dev.txt):

```bash
pip install -r requirements-dev.txt
```

Sin `make` instalado —Windows no lo trae— los dos objetivos **son** estos dos comandos:

```bash
python scripts/dev.py                       # equivale a `make dev`
python scripts/probar_e2e.py                # equivale a `make probar`
```

`make dev` ([`scripts/dev.py`](scripts/dev.py)) genera el dataset si falta, fija
`MODO_ALMACENAMIENTO=memoria`, busca un puerto libre si el 8000 está ocupado, arranca
`uvicorn --reload` y abre la consola en `/ui` (con reserva a `/docs`).

`make probar` ([`scripts/probar_e2e.py`](scripts/probar_e2e.py)) recorre token → hechos →
explicar en las dos verbosidades → evidencia → derivación → auditoría → cadena de hashes →
modo adversario, e imprime **PASA** o **FALLA** por paso. Sale con **1** si algo falla y con
**2** si la API no responde. Solo usa la biblioteca estándar:

```
  [PASA ] hechos conciliados                 Δ=2082 c · residual=0 c · líneas=4 · sha256=3227801e4fcc
  [PASA ] explicar CORTO                     PASS · 12/12 ancladas · 5 bloques · modo=LLM
  [PASA ] cadena de hashes (JSONL local)     29 eventos · íntegra=True · último=8baa77114376
  [PASA ] modo adversario caza la cifra      limpio=PASS · envenenado=FAIL · infractores=['S/ 28.13']
==============================================================================
TODO PASA · 19/19 pasos en 0.56 s
==============================================================================
```

**Qué degrada y cómo.** `MODO_ALMACENAMIENTO=memoria` (lo que fija `make dev`) hace que no se
abra **ninguna** conexión: el índice vectorial vive en el proceso, el dataset se lee de
`data/sintetico/` y la bitácora encadenada es un JSONL en `data/auditoria/eventos.jsonl`. Con
`auto` —el valor por defecto— se usa PostgreSQL solo si `DATABASE_URL` trae valor, y si no
responde se degrada a memoria avisando. Sin `GEMINI_API_KEY` se usa el proveedor
determinístico. **Nada falla en cascada**: cada degradación se anuncia en el log y en
`/salud/preparacion` (`almacenamiento` dice qué se pidió; `rag.vectorial.respaldo`, qué se
consiguió).

### Objetivos del `Makefile`

| Objetivo | Qué hace | Dónde corre |
|---|---|---|
| `dev` | Dataset si falta + `uvicorn --reload` + abre `/ui` | Local, **sin Docker ni PostgreSQL** |
| `probar` | Recorrido end-to-end con PASA/FALLA por paso | Local, contra la API levantada |
| `up` / `down` / `ps` / `logs` | Ciclo de vida de los contenedores | Docker |
| `migrate` | Migraciones SQL idempotentes | Docker |
| `seed` | Dataset sintético determinístico | Docker |
| `indexar` | Índice RAG en pgvector | Docker |
| `demo` | Todo lo anterior + `smoke` | Docker |
| `smoke` | Explica `C-DEMO-01` y exige `PASS` | Docker (o local con `SMOKE=`) |
| `eval` | Las 3 métricas oficiales | Intérprete local |
| `audit` | Verifica la cadena de hashes y muestra el último turno | Intérprete local |
| `test` / `lint` / `fmt` | Pruebas, estilo, formato | Intérprete local |
| `limpiar` / `limpiar-datos` | Borra contenedores y cachés / el dataset | — |

---

## 4. Cómo se corre la evaluación

```bash
make eval                    # tabla en terminal, con la advertencia de circularidad
make eval ARGS="--markdown"  # para pegar en el documento ejecutivo
make eval ARGS="--json"      # para CI
make eval ARGS="--casos G01_demo_cambio_plan_adelantada --detalle"
```

Devuelve **0** si la evaluación aprueba, **1** si alguna métrica incumple, **2** si falta el
dataset. El criterio de aprobación está fijado en código
([`eval/metricas.py`](eval/metricas.py)): `TA_respuesta = 0`, sin fragmentos prohibidos, sin
falsos negativos de hand-off, invariante exacto en todos los casos y *strict answer
accuracy* = 100 %.

Los **261 casos golden** viven en [`eval/golden/*.yaml`](eval/golden/) con cifras tomadas del
dataset generado por la semilla `20260804`. Los 38 primeros están escritos a mano, uno a uno,
porque cada uno documenta una decisión concreta (el guion de la demo, la atribución causal, los
tres adversariales originales). Los 223 restantes los produce
[`eval/generar_golden.py`](eval/generar_golden.py) por **muestreo estratificado y reproducible
por semilla**: 8 escenarios × 2 modalidades de renta × 2 verbosidades × 4 canales, ~30 % de casos
compuestos —la proporción del dataset—, las dos direcciones del delta, con deuda arrastrada y sin
ella, la cuota de equipo financiado en sus tres tramos, los controles de longitud de ciclo y
**16 adversariales de inyección de prompt**, uno por familia de señal reconocida.

El tamaño no es cosmético: con 34 casos, «`TA_respuesta` = 0,00 %» es compatible con una
alucinación cada cien respuestas (0,99³⁴ = 71 % de probabilidad de no verla). Con 261 esa
probabilidad baja al 7 %.

Y la prueba que hace fallar la build está en
[`tests/golden/test_sin_numeros_no_anclados.py`](tests/golden/):

```python
infractores = extraer_numeros(resp.texto) - fs.tokens_permitidos()
assert infractores == set(), f"Alucinación numérica: {infractores}"
```

### Ver al verificador trabajar

Un `PASS` no prueba nada si nunca se ha visto un `FAIL`. Con `ENTORNO=dev`:

```bash
curl -sX POST $API/dev/alucinar -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"activar":true,"delta_cent":731}'
```

Inyecta una cifra inexistente en una explicación ya generada. El verificador la caza, la
respuesta se bloquea, el turno termina en derivación y el log de terminal muestra
`NO ANCLADAS 1`. La cifra inventada **no llega al cliente**.

---

## 5. Mapa de módulos

```
recibo-claro/
├─ apps/
│  ├─ api/                    FastAPI: routers, seguridad por niveles y el ACL
│  │  ├─ acl.py               ← Anti-Corruption Layer con BrainyBill y Amdocs
│  │  ├─ security.py          ← JWT, matriz LOA0/LOA1/LOA2/LOA_ASESOR, redacción
│  │  └─ routers/             salud · hechos · explicar · evidencia · derivacion
│  │                          auditoria · catalogo · dev
│  └─ mocks/                  BrainyBill y Amdocs (CRM) como servicios HTTP reales
├─ packages/
│  ├─ core_domain/            Modelo canónico. Céntimos enteros, Pydantic v2,
│  │                          FactSet sellado, RespuestaCanalAgnostica
│  ├─ facts_engine/           ★ El motor determinístico
│  │  ├─ tramos.py            partición del ciclo por eventos
│  │  ├─ prorrateo.py         renta adelantada/vencida, sistema francés
│  │  ├─ diff.py              comparación recibo actual vs. previo
│  │  ├─ atribucion.py        concepto → causa oficial, con confianza
│  │  ├─ invariante.py        conciliación al céntimo
│  │  ├─ confianza.py         umbral de incomprensión (hand-off)
│  │  └─ motor.py             fachada: recibos → FactSet
│  ├─ llm_layer/              Capa generativa y verificador
│  │  ├─ providers/           Gemini · Mock determinístico
│  │  ├─ prompts/             el prompt de 4 bloques
│  │  ├─ plantillas/          10 plantillas Jinja por causa dominante
│  │  ├─ verificador.py       ★ el anclaje numérico
│  │  └─ generador.py         orquestación, reintento y degradación
│  ├─ retriever/              RAG: saneador · BM25 · pgvector · fusión RRF
│  ├─ governance/             auditoría encadenada · tasa de silencio
│  └─ datagen/                generador sintético + ACL del dataset real
├─ db/                        migraciones SQL y rules.yaml (reglas de negocio)
├─ eval/                      protocolo de evaluación y 261 casos golden
├─ tests/                     unit · propiedad · contrato · golden
├─ docs/                      arquitectura · ADR · declaración de herramientas
├─ ejemplos/curl.md           recorrido completo de la demo
└─ docker/smoke.py            el guardián de `make demo`
```

**Dos ficheros mandan sobre el resto:**

- [`db/reglas/rules.yaml`](db/reglas/rules.yaml) — **toda** la parametrización de negocio
  (política de prorrateo, IGV, confianzas, umbrales, catálogo de 31 conceptos, tabla
  `regla_concepto_causa`). El motor no tiene constantes de negocio en el código; la versión de
  reglas viaja dentro de cada respuesta.
- [`packages/datagen/mapping/movistar_map.py`](packages/datagen/mapping/movistar_map.py) — el
  ACL declarativo. **Es el único fichero que cambia cuando llegue el dataset real de Movistar.**

---

## 6. Tabla de cumplimiento

Cada fila es un requisito **literal** de la ficha del Desafío 1 o de las BASES, y el archivo o
módulo donde está implementado. Lo que no está hecho aparece marcado como tal.

Estado: ✅ implementado y verificado · 🟡 parcial (se dice hasta dónde) · ⛔ no implementado.

### 6.1 Objetivo y capacidades (ficha §«Objetivo» y §«Propuestas que se valorarán»)

| # | Requisito literal | Estado | Dónde está |
|---|---|---|---|
| 1 | *«analizar recibo actual + previos»* — BrainyBill expone actual + **cinco** previos | ✅ | `apps/api/acl.py::AdaptadorBrainyBill` · `packages/facts_engine/motor.py::seleccionar_recibo_previo` · mock en `apps/mocks/brainybill/` |
| 2 | *«identificar las causas más probables de variación»* | ✅ | `packages/facts_engine/diff.py` (FULL OUTER JOIN por concepto) + `atribucion.py` (tabla `regla_concepto_causa`, 3 ramas de confianza) |
| 3 | *«explicar conceptos en lenguaje simple»* | ✅ | Catálogo de 31 conceptos en `db/reglas/rules.yaml` + `packages/datagen/catalogo_seed.py`; endpoint `GET /v1/catalogo/{concepto_id}` |
| 4 | *«sugerir siguientes acciones»* | ✅ | `packages/llm_layer/generador.py::ETIQUETAS_ACCION` + `apps/api/routers/explicar.py` |
| 5 | *«derivar a asesor humano con contexto cuando corresponda»* | ✅ | `packages/facts_engine/confianza.py` (decisión) + `apps/api/routers/derivacion.py` (`context_ref`, brief de 7 líneas, `factset_sha256`) |
| 6 | *«habilitar cross-selling personalizado y contextual cuando sea pertinente, sin forzar una oferta en todos los casos»* | 🟡 | `apps/api/routers/explicar.py::evaluar_cross_selling` + `cross_selling` en `rules.yaml`. Se emite la **acción** `VER_ALTERNATIVAS`, sin texto ni cifras comerciales: no disponemos de catálogo de ofertas real. Ver §7 |
| 7 | *«prototipo funcional de chatbot / asistente conversacional generativo … con tono humano, transparente y horizontal, evitando estructuras robóticas»* | 🟡 | `POST /v1/explicar` responde en lenguaje natural (`packages/llm_layer/`), con jitter léxico determinístico y dos verbosidades. **Sin interfaz gráfica**: se entrega como API. Ver §7 |
| 8 | *«comparar recibo actual vs. anteriores y detectar diferencias por: cambio de plan, equipo financiado, compra de paquetes, cargos adicionales, promociones vencidas, notas de crédito/débito, prorrateos, reconexiones o ajustes por días de suspensión»* — **las 9 causas** | ✅ | `packages/core_domain/enums.py::CausaOficial` (9 valores) + `MAPA_MOVIMIENTO_A_CAUSA_OFICIAL`; motor en `facts_engine/`; los 8 escenarios generados en `packages/datagen/escenarios.py` |
| 9 | *«explicación clara, visual y no técnica»* | 🟡 | Bloques tipados `texto · kv · puente · tabla · aviso` (`core_domain/esquemas/respuesta.py`). El bloque `puente` es una **cascada** lista para graficar. El render visual corresponde a la App/Bot. Ver §7 |
| 10 | *«recomendación de siguientes acciones: pagar, revisar el detalle, registrar la consulta, revisar alternativas comerciales, o derivar a asesor con contexto»* — las 5 | ✅ | `AccionSiguiente` = `PAGAR · VER_DETALLE · REGISTRAR_CONSULTA · VER_ALTERNATIVAS · DERIVAR_ASESOR` en `packages/core_domain/enums.py` |
| 11 | *«flujos de derivación inteligente (hand-off) … transfiriendo el contexto de la interacción»* | ✅ | `apps/api/routers/derivacion.py::construir_contexto_derivacion` — el asesor recibe ficha, `FactSet` sellado y la explicación ya entregada, para no repetirla |
| 12 | **«Efecto efervescente»**: *«cerrar la interacción recordando proactivamente el diferencial comercial y los beneficios con los que YA CUENTA el cliente en su plan actual, SIN presentarlos como adiciones nuevas»* | ✅ | `efecto_efervescente` en `rules.yaml` (frase de apertura, máx. 2 beneficios) + macro `cierre` en `packages/llm_layer/plantillas/_comun.jinja` |
| 13 | **«Cross-selling restrictivo»**: *«activado única y exclusivamente si el modelo clasifica la consulta original como RESUELTA POSITIVAMENTE y existe una REGLA DE NEGOCIO EXPLÍCITA que lo habilite»* | ✅ | Doble condición implementada en `evaluar_cross_selling(…, resuelta, derivar)`; guardas adicionales en `rules.yaml`: prohibido si hay derivación, si el delta es negativo o si la confianza < 0.90 |
| 14 | *«dar pase al asesor humano con contexto si no logra resolver la duda, siempre con 0 % de alucinaciones»* | ✅ | `packages/llm_layer/verificador.py` bloquea y deriva; medido como `TA_respuesta = 0` en `make eval` |
| 15 | *«uso responsable de IA, protección de datos y autenticación para el acceso a información sensible»* | ✅ | `apps/api/security.py` (JWT HS256, matriz de niveles, `redactar_para_nivel`); `docs/declaracion_herramientas.md`; `data/` fuera del control de versiones |

### 6.2 Métricas e indicadores (ficha §«Indicadores»)

| # | Requisito literal | Estado | Dónde está |
|---|---|---|---|
| 16 | *«Precisión de Recuperación (Retrieval Accuracy): capacidad del modelo para extraer el dato exacto de la base proporcionada»* | ✅ | `eval/metricas.py::metricas_recuperacion` — tres capas: field-level exacto en céntimos (micro/macro), Recall@1 doc-level y **strict answer accuracy** como titular |
| 17 | *«Tasa de Alucinación: cero invenciones financieras COMPROBABLES MEDIANTE LOGS DE LA TERMINAL»* | ✅ | `packages/llm_layer/verificador.py` + `packages/governance/auditoria.py::formatear_para_terminal` (6 líneas por turno con `AFIRMACIONES NUMÉRICAS n · ANCLADAS n · NO ANCLADAS 0`) + `GET /v1/auditoria` |
| 18 | *«Precisión del Hand-off: exactitud lógica al decidir cuándo derivar a un humano basándose en UMBRALES DE INCOMPRENSIÓN»* | ✅ | `packages/facts_engine/confianza.py` (reglas duras + score continuo `U` con histéresis) · métricas en `eval/metricas.py::metricas_handoff` (recall primaria, precisión, F2, tasa de atrapamiento, completitud) |
| 19 | *«incorporar un mecanismo para clasificar el nivel de satisfacción o "TASA DE SILENCIO POST-EXPLICACIÓN" (si el cliente entendió y cerró la sesión)»* | ✅ | `packages/governance/telemetria.py` — sonda por turno, ventana configurable, y **el abandono ambiguo no cuenta como éxito**: se publican cota inferior, cota superior y la banda de incertidumbre |
| 20 | *«escalabilidad para picos de hasta 3 veces la volumetría normal»* | 🟡 | Dimensionamiento con el cálculo a la vista en `docs/arquitectura.md` §6; arranque en frío resuelto en el `lifespan` de `apps/api/main.py`. **No se ha ejecutado prueba de carga.** Ver §7 |
| 21 | Indicadores de negocio (llamadas −15 %, NPS +10 %, reclamos −5 %) | ⛔ | Solo medibles en producción. La telemetría que los alimentaría (`silence_probe_id`, tasa de derivación, contactos repetidos) ya se emite; el cálculo del indicador es de Movistar |

### 6.3 Enfoque de IA y demo (ficha §«Enfoque de IA esperado» e §«Información adicional»)

| # | Requisito literal | Estado | Dónde está |
|---|---|---|---|
| 22 | *«arquitectura RAG»* | ✅ | `packages/retriever/` — tres corpus con acceso distinto: catálogo por clave, FAQs híbrido BM25+vectorial con fusión RRF (k=60), casuísticas por firma causal |
| 23 | *«respuestas limitadas estrictamente a la base de datos de facturación provista, para garantizar 0 % de alucinaciones financieras, apoyándose en reglas / base de conocimiento»* | ✅ | El conjunto permitido del verificador se construye **solo** desde el `FactSet` (`verificador.py::construir_permitidos`); `packages/retriever/saneador.py` garantiza que el texto recuperado **no contiene un solo dígito** |
| 24 | *«experiencia omnicanal App + Bot (+ WhatsApp)»* | ✅ | `RespuestaCanalAgnostica` con bloques que cada canal degrada; `Canal` = `APP·BOT·WHATSAPP·ASESOR`; WhatsApp resuelto por nivel de aseguramiento (§6.4) |
| 25 | *«motor de recomendación de siguientes acciones: pago, consulta, derivación con contexto o propuesta comercial personalizada cuando aplique»* | ✅ | `packages/llm_layer/generador.py::accion_sugerida` + reglas de `explicar.py` |
| 26 | *«usando datos simulados o anonimizados y reglas de negocio simplificadas»* | ✅ | `packages/datagen/` (300 clientes, 1 800 recibos, ground truth escrito en el mismo acto) + `db/reglas/rules.yaml`. **Sin PII: ni DNI ni teléfono** (hay un `CHECK` en `db/migraciones/001_core.sql` que los rechaza) |
| 27 | *«categorizando los motivos de consulta en lenguaje cliente alineado al de la atención humana Movistar (ej. prorrateos, reconexiones)»* | ✅ | `ETIQUETAS_CAUSA_OFICIAL` en `packages/core_domain/enums.py` — las 9 etiquetas son las de la ficha, literales |
| 28 | *«no mostrar información sensible sin autenticación; contemplar protección de datos, TRAZABILIDAD y uso responsable de IA»* | ✅ | `security.py` + bitácora encadenada por hash (`packages/governance/auditoria.py`, `db/migraciones/003_auditoria.sql`, con `UPDATE`/`DELETE` bloqueados por *trigger*) |
| 29 | **Demo:** *«demostración funcional EN VIVO abordando al menos DOS de: (a) prorrateos, (b) cuota de equipo financiado, (c) reconexión tras suspensión morosa, (d) fin de descuentos, (e) cambios de plan, todo en ambas modalidades de RENTA ADELANTADA y VENCIDA»* | ✅ | **Los cinco**, en ambas modalidades: `eval/golden/02_escenarios_adelantada.yaml` y `03_escenarios_vencida.yaml`; guion de 5 minutos en `ejemplos/curl.md` §16; clientes de guion `C-DEMO-01` (ADELANTADA), `C-DEMO-02` y `C-DEMO-03` (VENCIDA) |
| 30 | *«modelo escalable a todo el ecosistema digital de Movistar, con retroalimentación para mejora continua»* | 🟡 | La telemetría de silencio y la bitácora de auditoría son el insumo del bucle de mejora. **El bucle en sí (reentrenamiento, curación de FAQs a partir de derivaciones) no está implementado.** Ver §7 |
| 31 | *«sistemas y canales involucrados: App Mi Movistar · Bot Lucía · WhatsApp Movistar · Amdocs (CRM) · el sistema facturador · BrainyBill»* | 🟡 | BrainyBill y Amdocs integrados vía ACL con mocks HTTP funcionales (`apps/api/acl.py`, `apps/mocks/`). App, Bot y WhatsApp son consumidores de la API. **El sistema facturador no se integra**: no hay contrato publicado y el recibo ya llega por BrainyBill. Ver §7 |

### 6.4 Autenticación por canal — el caso WhatsApp

`[PROPUESTA]` La ficha exige *«no mostrar información sensible sin autenticación»* pero también
atención por WhatsApp, donde el nivel de aseguramiento de la identidad es bajo. Se resuelve con
una matriz explícita, no negando el canal (`apps/api/security.py`):

| Nivel | Canal típico | Qué ve | Verificado en |
|---|---|---|---|
| `LOA0` | anónimo | solo el catálogo de conceptos | `GET /v1/catalogo` |
| `LOA1` | **WhatsApp** | existencia, dirección y causa del cambio; **ningún importe** | `redactar_para_nivel` — el texto entregado **no contiene un solo dígito** |
| `LOA2` | App Mi Movistar | explicación completa con importes | `POST /v1/explicar` |
| `LOA_ASESOR` | Call center 104 | como LOA2 + `acting_on_behalf_of` obligatorio y registrado | `EventoAuditoria.acting_on_behalf_of` |

Comprobación en `ejemplos/curl.md` §10 y en `tests/contract/test_respuestas_api.py::test_el_nivel_loa1_no_entrega_importes`.

### 6.5 BASES (reglas del concurso)

| # | Requisito literal | Estado | Dónde está |
|---|---|---|---|
| 32 | *«El uso de IA deberá ser declarado, especificando herramientas y su rol en la solución»* | ✅ | [`docs/declaracion_herramientas.md`](docs/declaracion_herramientas.md) |
| 33 | *«El uso de datasets, API o servicios de terceros deberá ser declarado»* | ✅ | Misma tabla, con columna de licencia real y de si procesa datos de Movistar |
| 34 | *«Todo uso de terceros … debe cumplir estrictamente sus licencias»* | ✅ | Solo licencias permisivas (MIT / Apache-2.0 / BSD / PSF / PostgreSQL). **Ninguna GPL/AGPL.** Declaradas una por una en `pyproject.toml` y en la declaración de herramientas |
| 35 | *«la información, datos y archivos proporcionados por Movistar son confidenciales y no divulgables»* (10 años) | ✅ | `data/*` en `.gitignore` y en `.dockerignore`; `data/README.md` explica por qué el directorio está vacío; **ningún dato de Movistar entra al repositorio ni a la imagen** |

---

## 7. Lo que NO está hecho

Se lista aquí y no en letra pequeña. El detalle completo, con riesgos y responsables, está en
[`docs/pendientes.md`](docs/pendientes.md).

| Falta | Por qué | Impacto |
|---|---|---|
| **Corregir la atribución causal en escenarios compuestos** ← *el defecto que sí duele* | El generador y el motor etiquetan **todos** los deltas de un escenario con su causa principal | En `C-DEMO-01` la aritmética es exacta (residual 0, `PASS`) pero la narrativa dice *«subió porque cambió de plan»* cuando el cambio de plan le **ahorró S/ 32.26**: lo que subió el recibo fue el fin del descuento. Diagnóstico completo y plan de corrección en [`docs/pendientes.md`](docs/pendientes.md) §1, riesgo **R-07**. **Media jornada de trabajo** |
| **Interfaz gráfica** (App / Bot / WhatsApp) | Excluida del alcance por decisión del equipo: el valor está en el motor y en la garantía numérica, no en pintar bloques | La demo se hace con `curl` y con `/docs`. Los bloques tipados están listos para consumir |
| **Integración con el sistema facturador** | Sin contrato publicado; el recibo ya llega por BrainyBill | Ninguno para el prototipo; sería trabajo de ACL |
| **Catálogo comercial real** para el cross-selling | No forma parte de los datos del Desafío 1 | La acción `VER_ALTERNATIVAS` se emite sin oferta concreta; la doble condición sí está implementada |
| **Bucle de mejora continua** | Requiere histórico de producción | La telemetría que lo alimenta ya se emite |
| **Prueba de carga del pico 3×** | Falta entorno representativo | El dimensionamiento está calculado y documentado (`docs/arquitectura.md` §6), pero **es una estimación nuestra, no un dato de Movistar** |
| **Estado de conversación en Redis/Postgres** | Hoy vive en memoria del proceso (LRU 512) | Con varias réplicas, `/v1/evidencia` y `/v1/derivacion/{ref}` solo aciertan en la réplica que atendió el turno. Interfaz ya aislada en una clase |
| **Validación de reglas con Movistar** | Nadie del equipo de facturación las ha confirmado | Todo lo marcado `[POR VALIDAR]` en `db/reglas/rules.yaml`: cobro en suspensión, convención de prorrateo, cargo de reconexión, días de gracia |

---

## 8. Documentación

| Documento | Qué contiene |
|---|---|
| [`docs/arquitectura.md`](docs/arquitectura.md) | Diagramas de contexto, flujo y secuencia; el ACL; el posicionamiento como *skill* del Bot Lucía; niveles por canal; dimensionamiento del pico 3× con el cálculo a la vista; separación tabular vs. vectorial del RAG |
| [`docs/declaracion_herramientas.md`](docs/declaracion_herramientas.md) | La tabla que exige BASES §10: herramienta, tipo, versión, licencia, rol exacto, si procesa datos de Movistar y dónde se ejecuta |
| [`docs/ADR/`](docs/ADR/) | Cuatro decisiones de arquitectura: por qué el recibo no se vectoriza · por qué céntimos enteros · por qué el LLM no calcula · por qué tramos y no fórmulas por escenario |
| [`docs/ELECCION_DEL_MODELO.md`](docs/ELECCION_DEL_MODELO.md) | Qué modelo generativo conviene y **por qué la elección pesa menos de lo que parece**: precios verificados, coste por explicación, la decisión según se priorice humanización, velocidad, coste o control, y qué invalidaría la recomendación |
| [`docs/pendientes.md`](docs/pendientes.md) | Lo que falta, lo `[POR VALIDAR]` con Movistar y los riesgos abiertos |
| [`ejemplos/curl.md`](ejemplos/curl.md) | Recorrido completo de la demo, 16 secciones, con los tres clientes de guion |
| [`data/README.md`](data/README.md) | Por qué `data/` está vacío en el repositorio |

---

## 9. Configuración

Todas las claves están en [`.env.example`](.env.example). Ninguna es obligatoria para la demo:
los valores por defecto arrancan el sistema completo en modo determinístico y sin red.

| Variable | Por defecto | Para qué |
|---|---|---|
| `ENTORNO` | `dev` | Con `dev` se monta `/dev/token` y `/dev/alucinar`. **En producción no** |
| `MODO_ALMACENAMIENTO` | `auto` | `memoria` \| `postgres` \| `auto`. Único interruptor de PostgreSQL |
| `DATABASE_URL` | vacía | Solo se usa con `postgres` o `auto`. `docker compose` la inyecta |
| `LLM_MODE` | `mock` | `mock` (determinístico, sin red) o `gemini` |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | vacías | Nunca se commitean. El id de modelo se lee del entorno: **`[POR VALIDAR]` contra la documentación vigente de Google** |
| `VERIFICADOR_ESTRICTO` | `true` | Bloquear la respuesta ante una cifra sin anclar |
| `DEMO_SEED` | `20260804` | Determinismo del dataset |
| `COBRO_EN_SUSPENSION` | `false` | **`[POR VALIDAR]` con el equipo de facturación** |
| `CONVENCION_PRORRATEO` | `actual` | `actual` \| `30_360`. **`[POR VALIDAR]`** |

---

## 10. Licencia y autoría

Código bajo licencia MIT. Las dependencias, todas permisivas, están declaradas con su licencia
real en [`docs/declaracion_herramientas.md`](docs/declaracion_herramientas.md).

`[CONFIRMADO-OFICIAL]` Conforme a las BASES, la inscripción *«implica cesión de los derechos de
PI sobre las propuestas a favor de Integratel, permitiendo su desarrollo y utilización futura.
Se reconoce la autoría de los equipos.»*

**Este repositorio no contiene ningún dato proporcionado por Movistar.** Todo el dataset es
sintético y reproducible desde la semilla `20260804`.
