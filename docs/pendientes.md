# Pendientes, supuestos abiertos y riesgos

Corte: **8 de agosto de 2026**. Rehace las §1, §3.2, §3.3, §3.5, §4 y §6 tras la ronda de
correcciones de ese día; la §2 y la §5 vienen del corte del 5 de agosto y no han cambiado.
Postulación (Fase 3): **domingo 16 de agosto, 23:59**.

**Los tres defectos que este documento declaraba abiertos están cerrados y verificados.** El
resumen, para no tener que leer las cuatro secciones:

| Defecto | Estado el 8 de agosto | Dónde se comprueba |
|---|---|---|
| **1 · Atribución causal engañosa** en escenarios compuestos | ✅ **cerrado.** `DESCUENTO_PROMOCIONAL` se atribuye a `FIN_DESCUENTO`, las causas se agregan por separado y el texto nombra el fin del descuento como causa del aumento | §1 · `tests/golden/test_atribucion_causal.py` · casos golden `G35`–`G38` |
| **2 · `rehidratacion.py` sin pruebas** | ✅ **cerrado.** 13 pruebas de integración que cruzan la frontera del disco y un proceso nuevo | §3.5 · `tests/integracion/test_rehidratacion.py` |
| **3 · 34 casos golden son pocos** | ✅ **cerrado.** La suite tiene **261 casos**, y ampliarla **encontró un cuarto defecto real** ya corregido (§1.1) | §3.3 · `eval/generar_golden.py` |

Lo que **sigue abierto** no ha cambiado de naturaleza: el multi-réplica (R-03), la prueba de
carga, la caché del ACL, la política de borrado del fichero de *checkpoints* y la circularidad de
la evaluación (R-04), que solo cierra el equipo de facturación de Movistar.

Este documento existe para que nadie —ni el jurado, ni quien retome el proyecto— tenga que
descubrir por su cuenta lo que falta. Todo lo verificable de aquí se ha comprobado contra el
código y contra el dataset del día del corte.

Etiquetado: `[CONFIRMADO-OFICIAL]` · `[SUPUESTO]` · `[PROPUESTA]` · `[POR VALIDAR]`.

---

## 1. Defecto corregido — atribución causal en escenarios compuestos

**Era la prioridad más alta:** el único defecto que afectaba a la calidad de la explicación al
cliente. **Detectado el 5 de agosto de 2026, corregido y verificado el 8 de agosto de 2026**
ejecutando el motor y la API sobre `C-DEMO-01`.

### Qué pasaba

En `C-DEMO-01` (renta adelantada, cambio de plan a mitad de ciclo) el motor asignaba **la misma
causa a las tres líneas** y agregaba los signos contrarios en un neto:

| Concepto | Δ | Causa **antes** | Causa **ahora** |
|---|---|---|---|
| `DESCUENTO_PROMOCIONAL` (desapareció) | **+49.90** | `CAMBIO_PLAN` | **`FIN_DESCUENTO`** (confianza 0,98, movimiento 53336293) |
| `RENTA_PLAN_MOVIL` (plan más barato) | −20.00 | `CAMBIO_PLAN` | `CAMBIO_PLAN` |
| `AJUSTE_RETROACTIVO_RENTA` (nuevo, a favor) | −12.26 | `CAMBIO_PLAN` | `CAMBIO_PLAN` |
| `IGV` | +3.18 | derivado, sin causa | derivado, sin causa |
| **Causas agregadas** | **+20.82** | *cambio de plan, S/ 17.64* — **una sola** | *promociones vencidas +49,90* · *cambio de plan −32,26* · *IGV +3,18* |

La explicación resultante decía:

> «Su recibo llegó S/ 20.82 más alto porque **cambió de plan a mitad de mes**.»

**Era engañoso.** El cambio de plan, por sí solo, hizo que el recibo **bajara S/ 32.26**. Lo que
lo subió fue el fin del descuento promocional atado al plan anterior. La aritmética siempre fue
correcta —residual 0, verificación `PASS`, invariante `OK`—; la narrativa causal no lo era. Y esa
es exactamente la segunda llamada al 104, que es el indicador que el desafío busca reducir.

### Qué se hizo, en los cuatro frentes que el defecto exigía

| # | Frente | Cambio |
|---|---|---|
| 1 | **Generador** — `packages/datagen/escenarios.py` | La variante `BAJADA_PIERDE_DESCUENTO` de `CambioPlanMedioCiclo` emite **dos** movimientos, `CAMBIO_PLAN` y `FIN_DESCUENTO`, y el ground truth del descuento pasa a `FIN_DESCUENTO`. Era la raíz: el generador etiquetaba **todos** los deltas de un escenario con la causa principal del escenario |
| 2 | **Motor** — `db/reglas/rules.yaml` + `packages/core_domain/reglas.py` + `packages/facts_engine/atribucion.py` | Sección nueva `preferencia_causa` (dato de reglas, **no** heurística en código): la desaparición o subida de un `DESCUENTO_*` prefiere `FIN_DESCUENTO` sobre el movimiento más cercano en el tiempo. Sin respaldo del CRM la confianza es 0,90; con respaldo, 0,98. Los movimientos descartados quedan citados en `evidencia` |
| 3 | **Narrativa** — `packages/facts_engine/motor.py` y `packages/llm_layer/plantillas/` | Las causas se ordenan por impacto absoluto y **se separan por signo**: los signos ya no se compensan. La causa principal se elige **con el signo del delta total**, por eso el fin del descuento gana al ahorro mayor |
| 4 | **Evaluación** — `eval/golden/08_atribucion_causal.yaml` | `G35` (C-DEMO-01 adelantada), `G36` (vencida), `G38` (clase `SUBIO`, descuento prorrateado) y `G37`, que es el **caso inverso**: un cambio de plan sin promoción que debe seguir siendo `CAMBIO_PLAN`. Sin ese cuarto caso, la corrección podría pasarse de frenada sin que nadie se enterara |

### El texto que produce hoy

> «Su recibo de julio de 2026 le llegó S/ 20.82 más caro que el de junio de 2026 porque **se le
> venció el descuento** que tenía y volvió al precio de lista. […] Se le terminó un descuento de
> S/ 49.90: la promoción llegó a su última mensualidad y ya no figura en este recibo. **A la vez,
> cambió a un plan más barato, lo que le ahorró S/ 32.26** entre la renta del mes que viene y el
> ajuste de los días ya cobrados. IGV: le agrega S/ 3.18 en este recibo. **Sumando y restando**,
> su recibo quedó S/ 20.82 más caro que el de junio de 2026.»

Verificación numérica `PASS`, 15/15 aserciones ancladas en `CORTO` y 28/28 en `DETALLE`,
invariante 0.

### Por qué la evaluación no lo detectaba

`precision_causa_raiz` reportaba 100 % porque el `ground_truth.csv` compartía el mismo criterio
equivocado que el motor:

```
C-DEMO-01,2026-07,DESCUENTO_PROMOCIONAL,CAMBIO_PLAN,4990,...
                                        ^^^^^^^^^^^ ahora dice FIN_DESCUENTO
```

Es la circularidad que la propia salida de `run_eval` advierte, materializada en un caso concreto.
**Solo apareció al leer el texto generado, no al mirar las métricas.** Hoy la misma métrica sigue
dando 100 % (391/391), pero **sobre una verdad correcta**. Conviene no olvidar la lección: una
métrica al 100 % puede convivir con un defecto de producto, y por eso la advertencia de
circularidad se publica en vez de esconderse (R-04).

### 1.1 El defecto que apareció al ampliar la suite

Ampliar los casos golden de 34 a 261 (§3.3) destapó **el mismo tipo de mentira por otra puerta**,
esta vez en un dato de reglas y no en una heurística: en `db/reglas/rules.yaml`,
`RENTA_MOVISTAR_TOTAL` era **el único concepto de renta sin `SUSPENSION`** entre sus causas
permitidas. A un cliente convergente al que le habían cortado el servicio, la renta se quedaba sin
causa del CRM (confianza 0,30) y heredaba la causa oficial del catálogo, así que el recibo decía
literalmente **«Cambio de plan: −S/ 49.01»** a quien nunca cambió de plan.

Son **9 cuentas de 300** y **ninguna caía en los 34 casos originales**. Arreglo: añadir
`SUSPENSION` al final de la fila, en la misma posición que en las otras cuatro rentas, para que
con cambio de plan **y** suspensión en el mismo ciclo siga mandando el cambio de plan. Protegido
por `tests/unit/test_renta_convergente_suspension.py`, cuya primera prueba es **paramétrica sobre
todo concepto `RENTA_*`**: protege también a la renta que alguien añada mañana.

**Aviso que no se resuelve solo, y que alguien debe decidir:** `InformeEvaluacion.aprobado` de
`eval/metricas.py` **no incluye `precision_causa_raiz`**. Con este defecto puesto, `run_eval`
seguía imprimiendo *EVALUACIÓN APROBADA*; quien rompía la construcción era `pytest`. O la métrica
entra en el criterio de aprobación, o hay que decir en voz alta que el veredicto de `run_eval` no
la cubre.

### 1.2 Y una tercera, hallada al revisar el texto de la demo

Misma familia —cifra anclada, frase al revés— en la macro `lista_lineas` de
`packages/llm_layer/plantillas/_comun.jinja`, que ramificaba **solo por clase** y no por signo:

- «Ajuste del mes anterior **aparece por** S/ 12.26» de un abono **a favor** del cliente;
- «Descuento por permanencia **ya no se le cobra**» de un descuento, que no se cobraba: se aplicaba.

Ahora las cuatro combinaciones de clase y signo tienen su frase: *aparece por* / *aparece a su
favor por* / *ya no se le aplica* / *ya no se le cobra*. Fijado en
`tests/golden/test_atribucion_causal.py::TestElSignoDeCadaLineaEnElTexto`.

**Las tres comparten diagnóstico y conviene decirlo junto:** el verificador anti-alucinación
garantiza que **ninguna cifra se inventa**, y las tres pasaban su control. Ninguna era un error
aritmético. **La exactitud numérica no implica veracidad narrativa**, y solo se detectan leyendo
lo que lee el cliente.

---

## 2. Parámetros `[POR VALIDAR]` con Movistar

Ninguno está enterrado en el código: todos viven en `db/reglas/rules.yaml` o en `.env`, y la
`rules_version` viaja dentro de cada respuesta.

| Parámetro | Dónde vive | Valor actual | Qué preguntar |
|---|---|---|---|
| `cobro_en_suspension` | `rules.yaml` | `false` | ¿Se cobra la renta durante los días de suspensión por deuda? |
| `convencion_prorrateo` | `rules.yaml` | `actual` | ¿Actual/actual o 30/360? **Se implementan ambas**; se elige la que cierra el invariante |
| `cargo_reconexion_cent` | `rules.yaml` | `2500` | ¿Importe vigente del cargo de reconexión? Hoy es un `[SUPUESTO]` |
| `dias_gracia_suspension` | `rules.yaml` | `15` | ¿Días antes de suspender por deuda? `[SUPUESTO]` |
| `igv_bp` | `rules.yaml` | `1800` | IGV al 18 %. ¿Hay conceptos exentos que el catálogo no marca? |
| `IMPORTES_EN_CENTIMOS` | `apps/api/acl.py` | `True` | ¿BrainyBill entrega céntimos enteros o soles decimales? |
| `FIN_CICLO_INCLUSIVO_EN_ORIGEN` | `apps/api/acl.py` | `False` | ¿El fin de ciclo del origen es inclusivo? Si lo fuera, **todos** los prorrateos se desplazan un día |
| Campos de BrainyBill por línea | ACL | supuestos | ¿Expone `period_from` y `period_to` **por línea**? Sin ese campo el prorrateo no es reconstruible con certeza y hay que resolverlo por inversión |
| Modalidad de renta predominante | — | ambas soportadas | ¿Cuál predomina en la planta B2C? |
| `GEMINI_MODEL` / `GEMINI_EMBED_MODEL` | `.env` | vacíos | **No se fija ningún identificador de modelo en el código.** Debe verificarse el vigente en la documentación de Google |
| «~1 millón de transacciones» de explicación en la App | ficha | asumido mensual | La ficha no declara el periodo. Afecta el dimensionamiento del pico 3× |
| Reclamo formal | — | no implementado | ¿La disconformidad con el monto expresada en el chat obliga a abrir reclamo formal bajo el reglamento OSIPTEL vigente? |
| Correspondencia canal → nivel de aseguramiento | `apps/api/security.py` | `[PROPUESTA]` | ¿WhatsApp con verificación adicional alcanza LOA2? Es configuración del emisor de tokens, no código |

---

## 3. Alcance no construido

### 3.1 Excluido por decisión del equipo

- **Frontend de producto.** El núcleo es *headless*: publica bloques tipados (`texto`, `kv`,
  `puente`, `tabla`, `aviso`) para que App Mi Movistar, Bot Lucía y WhatsApp los rendericen en su
  propio lenguaje; el bloque `puente` es una cascada lista para graficar. Lo que **sí** se entrega
  es una **consola de demostración** de 3 309 líneas —HTML, CSS y siete módulos ES nativos, sin
  `package.json`, sin `node_modules` y sin una sola CDN— en `apps/web/estatico/`, montada en `/ui`
  por la propia API (`apps/api/main.py:191`). Existe para enseñar el mecanismo, no para ser el
  producto: la API responde igual sin ella. Razonamiento completo en
  [`FUNDAMENTACION.md`](FUNDAMENTACION.md) §6.10. La demostración se apoya además en
  `ejemplos/curl.md`, la documentación viva en `/docs` y la vista de terminal de auditoría.

### 3.2 Ya implementado — no confundir con pendiente

Se listan porque en versiones anteriores de este documento aparecían como pendientes y **ya no
lo están**; conviene no arrastrar el error a la presentación:

| Antes pendiente | Estado real, verificado el 5 de agosto |
|---|---|
| Efecto efervescente | ✅ `efecto_efervescente` en `rules.yaml` + macro `cierre` en `packages/llm_layer/plantillas/_comun.jinja`. Aparece en la respuesta de `C-DEMO-01` |
| Cross-selling restrictivo | ✅ `evaluar_cross_selling` en `apps/api/routers/explicar.py` con la doble compuerta de la ficha, más las guardas de `rules.yaml` |
| Telemetría de tasa de silencio | ✅ `packages/governance/telemetria.py`, con la sonda abierta en cada respuesta y `silence_probe_id` publicado en `telemetria` |
| Verificación numérica y bitácora encadenada | ✅ 261 casos golden, 4 625 afirmaciones, `TA_respuesta = 0`; cadena verificable en `GET /v1/auditoria/cadena` |

### 3.3 Pendiente real — producto

| Pendiente | Por qué falta | Esfuerzo estimado |
|---|---|---|
| ~~Corregir la atribución causal de la §1~~ | ✅ **hecho el 8 de agosto de 2026.** Generador, motor, narrativa y evaluación; ver §1 | — |
| ~~Ampliar los casos golden de 34 a más de 200~~ | ✅ **hecho el 8 de agosto de 2026: 261 casos.** Generados por `eval/generar_golden.py` con muestreo estratificado reproducible por semilla, más los 38 escritos a mano, que no se tocan. Ampliar **encontró un defecto real** (§1.1), que era justamente el argumento | — |
| Catálogo comercial para el cross-selling | No forma parte de los datos del Desafío 1. Hoy la acción `VER_ALTERNATIVAS` se emite **sin oferta concreta** | Bloqueado por dato externo |
| Bucle de mejora continua (curar FAQs a partir de las derivaciones) | Requiere histórico de producción | Fuera del MVP |
| Emulador de WhatsApp con los límites reales del canal | Vale para el pitch, no para el motor | 0,5 jornada |
| Skill de Bot Lucía como emulador de *webhook* | Ídem: demuestra el posicionamiento sin cambiar el núcleo | 0,5 jornada |
| Las 10 diapositivas y el documento ejecutivo | Entregables de Fase 5 | — |

### 3.4 Pendiente real — infraestructura y escala

| Pendiente | Consecuencia hoy | Referencia |
|---|---|---|
| **Estado de conversación fuera del proceso** — **resuelto para la evidencia; abierto para el resto** | El endpoint ya delega en el grafo y el checkpointer escribe el turno completo en SQLite. `GET /v1/evidencia/{id}` **responde 200 tras matar y relevantar el proceso** (verificado con `taskkill /F`: mismos 24 items, mismo `sha256`, y 403 si el token es de otra cuenta). **Lo que sigue en RAM:** el historial de turnos, la histéresis `fue_derivada` y los contextos por `context_ref`, porque `MemoriaConversaciones` sigue siendo la fuente que leen los nodos. Con varias réplicas basta con que el segundo turno caiga en otra | [`ADR/005`](ADR/005-langgraph-para-la-orquestacion.md) · `arquitectura.md` §5.2 · §3.5 · riesgo **R-03** |
| **Caché del ACL** por `(cuenta_id, periodo)` | Cada turno hace dos llamadas a BrainyBill y Amdocs; el pico 3× se les traslada íntegro. El recibo de un mes cerrado **no cambia**, así que es cacheable sin riesgo de servir dato viejo | `arquitectura.md` §10 |
| **Caché de arquetipo narrativo** | Es la palanca que reduciría la dependencia del proveedor generativo por debajo del 20 % de los turnos. Hoy `tasa_fallback = 0 %`: todos los turnos pasan por el proveedor | `arquitectura.md` §10 · ADR 003 |
| **Prueba de carga** | El dimensionamiento del pico 3× es **aritmética sobre supuestos declarados**, con latencias medidas pero sin ensayo de carga. Ninguna cifra de escala debe presentarse como medición | `arquitectura.md` §10 |
| **Integración con el sistema facturador** | No hay adaptador. No es un problema para el prototipo: el recibo llega por BrainyBill | `arquitectura.md` §2 |
| **Gemini en el camino real** | La demo corre en `LLM_MODE=mock` por determinismo, y `google-genai` **no está instalada** en el entorno de referencia (los tests marcados `gemini` se omiten). El proveedor está implementado con import diferido y salida estructurada; falta ejercitarlo contra el servicio con una clave real | `packages/llm_layer/providers/gemini.py` |
| **Persistencia de FactSets y explicaciones en PostgreSQL** | Las tablas y sus `CHECK` existen (`db/migraciones/001_core.sql`), pero la API sirve desde memoria y disco. Migrar la escritura es directo; no se hizo por no añadir un fallo posible a la demo | `db/migraciones/001_core.sql` |

### 3.5 Abierto por la decisión del ADR 005 — capa de orquestación

**Verificado el 7 de agosto de 2026** leyendo el árbol y ejecutando la suite, en verde. La
intervención es aditiva y va por partes: hay piezas escritas y hay piezas que no.

### Ya escrito

| Pieza | Dónde | Comprobación ejecutada |
|---|---|---|
| Apagado de la telemetría de terceros, **con efecto al importar** | `packages/orquestacion/telemetria_externa.py` — fuerza los valores en vez de `setdefault` e invalida la caché `lru_cache` de `langsmith.utils.get_env_var` | `telemetria_externa_activa() -> False` |
| Checkpointer SQLite, degradación a `InMemorySaver` y lista blanca de tipos | `packages/orquestacion/checkpointer.py` — `abrir_checkpointer`, `obtener_checkpointer` (`@lru_cache(maxsize=1)`), `cerrar_checkpointer`, `tipos_permitidos` (92 tipos) | Dos procesos distintos (PID 20944 → 10908) comparten estado; el `FactSet` de `C-DEMO-01` vuelve del disco como `FactSet` con `sha256` idéntico `3227801e4fcca4c4` |
| `EstadoTurno` (se persiste) frente a `Servicios` (no se persiste) | `packages/orquestacion/estado.py` | — |
| Adaptador `LangChainProvider` como tercer modo | `packages/llm_layer/providers/langchain_.py` · `MODO_LANGCHAIN` y `MODOS_VALIDOS` en `providers/base.py:56,59` | `tests/unit/test_proveedor_langchain.py` |
| Ubicación del fichero fuera de Git y de la imagen | `CHECKPOINT_PATH`, por defecto `data/checkpoints/turnos.sqlite` | `data/*` en `.gitignore`, `data/` en `.dockerignore` |

### Pendiente

| Pendiente | Por qué importa | Estado |
|---|---|---|
| **Escribir el grafo** | `packages/orquestacion/grafo.py` existe y `POST /v1/explicar` delega en él: `ORQUESTADOR=grafo` es el valor por defecto y `ORQUESTADOR=directo` conserva la vía lineal como respaldo | ✅ hecho |
| **Escribirlo sin mover la bitácora** | La asimetría se conservó: la rama de intención y los errores del ACL siguen sin emitir `CHAIN`. `tests/unit/test_grafo.py` compara la secuencia de etapas rama por rama, y el snapshot de OpenAPI quedó **byte a byte idéntico** | ✅ hecho |
| **Enganchar `obtener_checkpointer()` al ciclo de vida** | Hecho: `calentar()` compila el grafo y abre el fichero al arrancar (y publica el diagnóstico en el log), y `cerrar_recursos()` cierra grafo y conexión en ese orden. Con `ORQUESTADOR=directo` no se importa LangGraph siquiera | ✅ hecho |
| **Pruebas propias de `packages/orquestacion/`** | `tests/unit/test_grafo.py` cubre el grafo, la degradación a memoria, la lista blanca y el round-trip del `FactSet`. `rehidratacion.py` **ya tiene batería propia**: `tests/integracion/test_rehidratacion.py`, 13 pruebas que escriben un turno con `SqliteSaver` sobre disco, abren una instancia nueva sobre el mismo fichero y exigen el `FactSet` de vuelta con `verificar_sha256()`, el 403 de cuenta ajena, el 404 fuera de la ventana de barrido, las cuatro degradaciones (destino imposible, SQLite corrupto, conexión cerrada, id vacío) y —en un **subproceso con intérprete limpio**— que la vía de respaldo no importa LangGraph. Verificadas por sabotaje: seis roturas deliberadas, seis rojos | ✅ hecho |
| **Los dos mapas que el `thread_id` no cubre** | `GET /v1/evidencia/{id}` **ya sobrevive al reinicio**: `packages/orquestacion/rehidratacion.py` busca el turno en el checkpointer y reconstruye el `RegistroExplicacion` (verificado con `taskkill /F`: 200, mismo `sha256`, y 403 para otra cuenta). Es un **barrido acotado** a los ~200 checkpoints más recientes, no un índice: un turno más viejo sigue dando 404, y `_contextos` por `context_ref` sigue sin rehidratarse | 🟡 parcial — falta el índice inverso `explicacion_id → thread_id` |
| **Política de retención y borrado del fichero** | Problema **nuevo**: antes el estado se perdía solo. Lo que se persiste incluye el `FactSet` volcado —`construir_contexto_derivacion` guarda `factset.model_dump(mode="json")` completo, `derivacion.py:235`—, es decir, importes de facturación. La ubicación ya está fuera de Git y de la imagen; **el ciclo de borrado no está decidido** | ⛔ pendiente |
| **Checkpointer PostgreSQL para varias réplicas** | SQLite es de un nodo. Resuelve el reinicio y la evidencia; **no** resuelve el multi-réplica salvo sobre volumen compartido. El cambio es de checkpointer, no de nodos | ⛔ pendiente — mantiene vivo **R-03** en su forma multi-réplica |
| **Declarar `langgraph` y sus dos paquetes de checkpoint en `pyproject.toml`** | Hecho: extra opcional `[orquestacion]` con los cuatro rangos (`langgraph>=1.2,<2.0`, `langchain-core>=1.5,<2.0`, `langgraph-checkpoint>=4.1,<5.0`, `langgraph-checkpoint-sqlite>=3.1,<4.0`), y el comentario del propio `pyproject.toml` deja escrita la prohibición de `langgraph-api`, `langgraph-cli` y LangSmith. [`declaracion_herramientas.md`](declaracion_herramientas.md) §2 actualizado en consecuencia | ✅ hecho |
| **`CHECKPOINT_PATH` en `.env.example` y `docker-compose.yml`** | Ambos ficheros llevan ya `ORQUESTADOR`, `CHECKPOINT_PATH`, `CHECKPOINT_DURABILITY` y el bloque `LANGSMITH_*` apagado | ✅ hecho |
| **Test de que el trazado externo está apagado** | `telemetria_externa_activa()` devuelve `False` hoy, pero nada hace fallar la build si mañana alguien lo enciende. Es el candidato natural a prueba de contrato | ⛔ pendiente |
| **Regla «ningún `emitir` antes de un `interrupt()`»** | Al reanudar, LangGraph **re-ejecuta el nodo entero**: un `auditoria.emitir` anterior al `interrupt()` duplicaría eventos en una bitácora *append-only* encadenada. Hoy no hay nada que impida escribirlo mal | ⛔ pendiente — candidato a test de estilo, como `test_sin_float.py` |
| **Coste del `Lock` del `SqliteSaver` bajo carga** | `cursor()` serializa las operaciones con un `threading.Lock`. 16 hilos concurrentes dieron 0 errores en 0,932 s, pero **eso no es una prueba de carga** contra el objetivo de ~15 rps de `arquitectura.md` §10 | `[POR VALIDAR]` — se suma a la prueba de carga ya pendiente |

**Lo que sí queda cerrado por el ADR 005:** la pregunta de *cómo* se saca el estado del proceso.
Estaba abierta desde la primera versión de este documento —«Redis o PostgreSQL», sin decidir— y
ahora tiene respuesta, con el módulo escrito y las pruebas ejecutadas que la respaldan
(`arquitectura.md` §5.2). **El defecto quedó cerrado a medias**: el endpoint ya delega en el grafo y
`GET /v1/evidencia/{id}` sobrevive al reinicio, pero el historial de turnos y la histéresis
`fue_derivada` los siguen leyendo los nodos desde `MemoriaConversaciones`, que sigue en RAM.

---

## 4. Riesgos abiertos

Numerados para poder citarlos desde el resto de la documentación.

| # | Riesgo | Severidad | Mitigación actual | Pendiente |
|---|---|---|---|---|
| **R-01** | **El dataset oficial no llega o llega tarde** — ningún documento fija fecha | Alta | El plan base es el dataset propio, determinístico y completo. El oficial entra por `packages/datagen/mapping/movistar_map.py`, único archivo que cambia | Preguntar a la organización (§5) |
| **R-02** | **Llega con esquema incompatible** o con recibos que no cuadran en origen | Media | ACL aislado + `validar()` que rechaza en la puerta + tests de contrato que corren idénticos contra mock y real | Ejercitar con el export real |
| **R-03** | **Memoria de conversación en el proceso** — **reducido.** `GET /v1/evidencia/{id}` ya sobrevive al reinicio; el historial de turnos y la histéresis de derivación, todavía no | Media | Un solo trabajador por contenedor, para no fragmentarla en silencio dentro de una réplica. El checkpointer está enganchado al ciclo de vida y el endpoint delega en el grafo ([`ADR/005`](ADR/005-langgraph-para-la-orquestacion.md)) | Leer el historial y `fue_derivada` desde el checkpoint en vez de desde RAM; índice inverso `explicacion_id → thread_id` para no depender de un barrido acotado; y checkpointer PostgreSQL para el multi-réplica —SQLite es de un nodo— |
| **R-09** | **Dependencia de un framework de terceros en el camino de una respuesta al cliente** | Baja | Los nodos solo **llaman** a funciones ya probadas: si LangGraph estorbara, se retira la capa de coordinación y el motor sigue calculando igual. Cuatro paquetes MIT, con `langgraph-api`, `langgraph-cli` y el servicio LangSmith excluidos, y el tracing apagado y comprobado con un cortafuegos de sockets | Rangos ya fijados en `pyproject.toml` (extra `[orquestacion]`). Sigue faltando el test de `tracing_is_enabled() is False` (§3.5) |
| **R-04** | **La circularidad de la evaluación** — ground truth y sistema comparten autor | Alta | Declarada en la salida de `run_eval`, en el README y en el pitch. En la demo se cede al jurado la elección del caso. La §1 de este documento es la prueba de que la advertencia no es retórica | Casos golden redactados por facturación |
| **R-05** | **Cuota o latencia del proveedor generativo durante la demo** | Media | `LLM_MODE=mock` corre sin red y es determinístico; la degradación a plantilla está implementada y anunciada con `X-Degradado`. Al menos uno de los ensayos debe hacerse en ese modo | Confirmar cuota real |
| **R-06** | **Fuga de datos de Movistar al repositorio** | Crítica | Repositorio privado; `data/` en `.gitignore` **y** en `.dockerignore`; la imagen no lleva datos. Confidencialidad de 10 años según BASES §9 | Revisión antes de cualquier publicación |
| **R-07** | **Narrativa causal engañosa** en escenarios compuestos — **cerrado el 8 de agosto de 2026** | Alta → **Baja residual** | Corregido en los cuatro frentes y protegido por regresión: `preferencia_causa` en `rules.yaml`, causas agregadas separadas por signo, `tests/golden/test_atribucion_causal.py` y los casos `G35`–`G38`, uno de ellos **inverso** para que la corrección no se pase de frenada. `precision_causa_raiz` = 100 % (391/391) sobre una verdad ya corregida | Lo residual es genérico y no tiene arreglo interno: **un escenario compuesto que el generador no imagine puede volver a producir una narrativa engañosa sin romper ninguna métrica**. Solo lo cierra el ground truth de facturación (R-04). `precision_causa_raiz` **no** entra en `InformeEvaluacion.aprobado`: decidir si debe (§1.1) |
| **R-08** | **Elegibilidad del equipo** | Fatal | Verificar que los 4 integrantes se inscribieron individualmente antes del 30 de julio, que el equipo es mixto y tiene ≥2 carreras distintas | **No tiene arreglo posterior** |

---

## 5. Preguntas a la organización

Pendientes de enviar a `concursos-cis@ulima.edu.pe`:

1. ¿Cuándo se entrega el dataset oficial del Desafío 1?
2. ¿El formulario de registro obliga a declarar un solo desafío?
3. ¿Cuánto dura el pitch del 27 de agosto? ¿Hay preguntas del jurado y cuántos minutos?
4. ¿Se presenta con laptop propia y proyector? ¿Hay red para invitados en la sala y algún
   requisito para conectarse? (Afecta a R-05: si no hay red, la demo va en modo `mock`.)
5. Sobre la volumetría: ¿el «~1 millón de transacciones» de explicación de recibo en la App es
   mensual? Es la entrada del dimensionamiento del pico 3×.

---

## 6. Cómo se comprueba lo que dice este documento

```bash
make test     # todo verde. El recuento crece con el proyecto: 357 el 5 de agosto, 454 el 7 y
              # 1 508 pasadas + 299 omitidas el 8 de agosto de 2026 (1 807 recogidas, ~50 s).
              # Las 299 omisiones tienen dos motivos, ambos legitimos y visibles con `pytest -rs`:
              # 261 «sin GEMINI_API_KEY» (la suite golden repetida contra el modelo real) y
              # 38 «el caso no declara fragmentos prohibidos»
make eval     # las 3 métricas oficiales, con la advertencia de circularidad
make audit    # verifica la cadena de hashes de la bitácora
make lint     # ruff check + format --check
```

El estado real de la §3.5 —qué está implementado y qué no— se comprueba con:

```bash
ls packages/orquestacion                              # grafo.py presente = grafo escrito y cableado
grep -rn checkpoint apps/api/deps.py                  # con resultados = enganchado al ciclo de vida
python -c "import importlib.metadata as m; print(m.version('langgraph'), m.version('langgraph-checkpoint'), m.version('langgraph-checkpoint-sqlite'), m.version('langchain-core'))"
python -c "import importlib.metadata as m; m.version('langgraph-api')"   # debe fallar: no se instala
python -c "from packages.orquestacion.telemetria_externa import telemetria_externa_activa as t; print(t())"   # False
```

La **corrección** de la §1 se comprueba con la misma orden con la que se reproducía el defecto.
Donde antes salían tres `CAMBIO_PLAN`, hoy sale `FIN_DESCUENTO` en la primera línea:

```bash
python -c "from eval.datos import cargar_cuenta, factset_de_cuenta; fs = factset_de_cuenta(cargar_cuenta('C-DEMO-01')); [print(l.concepto_id, l.delta_cent, l.causa) for l in fs.lineas]"
# DESCUENTO_PROMOCIONAL     4990  TipoMovimiento.FIN_DESCUENTO   <- antes CAMBIO_PLAN
# RENTA_PLAN_MOVIL         -2000  TipoMovimiento.CAMBIO_PLAN
# AJUSTE_RETROACTIVO_RENTA -1226  TipoMovimiento.CAMBIO_PLAN
# IGV                        318  None
```

Y el texto que lee el cliente, que es donde el defecto se veía y donde hay que mirar:

```bash
python -m uvicorn apps.api.main:app --port 8000 &   # LLM_MODE=mock ENTORNO=dev
python scripts/probar_e2e.py --api http://127.0.0.1:8000    # 19/19
```
