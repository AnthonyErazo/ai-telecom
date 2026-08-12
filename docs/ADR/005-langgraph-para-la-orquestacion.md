# ADR 005 — LangGraph para la orquestación, no para el cálculo

**Estado:** aceptada

**Alcance de la decisión:** dónde vive el control de flujo de un turno y dónde vive el estado de
la conversación. **No** cambia quién calcula ni quién verifica.

---

## Contexto

Dos hechos, ambos verificados el 7 de agosto de 2026 leyendo el código, no supuestos.

### 1. El control de flujo estaba escrito a mano en una sola función

`apps/api/routers/explicar.py` tiene **1194 líneas** (`wc -l`). La función `explicar_recibo`
ocupa las líneas 396–825 y contiene **cuatro `return`** —tres de ellos salidas tempranas— más
dos caminos que salen por excepción. En total, **once desenlaces distinguibles** para un mismo
turno:

| Camino | Dónde se decide | Cómo termina |
|---|---|---|
| 403 por cuenta cruzada | `cuenta_autorizada`, línea 421 | excepción, **sin un solo evento de bitácora** |
| No corresponde explicar el recibo | `not intencion.explica_recibo`, línea 465 | `_responder_por_intencion`, **sin evento `CHAIN`** |
| Intención sospechosa | dentro de la anterior, línea 914 | negativa fija; **no se llama al modelo** |
| Intención regulatoria o petición de humano | línea 953 | derivación con motivo propio |
| Error del ACL: 404, 422 o 503 | `construir_hechos`, línea 487 | excepción, **turno huérfano sin `CHAIN`** |
| Invariante roto | línea 512 | `_responder_derivando`, aviso crítico sin cifras |
| Retriever caído | `except Exception`, líneas 535-539 | **no corta**: se explica sin contexto |
| Verificación `FAIL` | línea 650 | `_responder_derivando` |
| Demo adversaria | `adversario.consumir()`, línea 611 | fuerza el camino anterior |
| Derivación por umbral de incomprensión | línea 676 | **no corta**: sigue y añade la derivación |
| Camino feliz | línea 825 | respuesta completa, redactada por nivel |

La función es correcta y está probada. El problema no es que falle: es que **las once ramas y
sus asimetrías viven en la lectura secuencial de un archivo**. La asimetría más delicada —dos de
los once caminos no emiten `CHAIN` porque no llaman a `_cerrar`— no está declarada en ninguna
parte; se descubre leyendo. Añadir un caso nuevo obliga a releer las 430 líneas para saber qué
compuertas hay que respetar.

### 2. La memoria de conversación moría con el proceso — este es el defecto real

`MemoriaConversaciones` (`apps/api/deps.py:251-345`) son cuatro estructuras **en RAM**:

| Mapa | Clave | Qué guarda | Recorte |
|---|---|---|---|
| `_explicaciones` | `explicacion_id` (== `trace_id`) | `RegistroExplicacion` con el `FactSet` y la respuesta | LRU 512 |
| `_turnos` | `conversation_id` | `list[Turno]`, `del historial[:-20]` (línea 297) | LRU 512 |
| `_contextos` | `context_ref` | el brief del asesor y el `FactSet` volcado | LRU 512 |
| `_derivadas` | — | `set[str]` de conversaciones ya derivadas | **sin recorte** |

Se sirven desde `@lru_cache(maxsize=1) def obtener_memoria()` (`deps.py:348`). **Un reinicio del
proceso las borra todas.** Consecuencias medibles, cada una con su línea:

- `GET /v1/evidencia/{explicacion_id}` responde **404 `EXPLICACION_NO_ENCONTRADA`**
  (`apps/api/routers/evidencia.py:156-163`). Es decir: el jurado pide la explicación, se reinicia
  el contenedor, y **la evidencia que respalda la cifra ya no existe**. Justo la trazabilidad que
  el proyecto ofrece como diferenciador.
- `POST /v1/derivacion` pierde `ultima_de_conversacion` (`derivacion.py:293`) y **recalcula** el
  `FactSet` en vez de reutilizar el del turno que motivó la derivación.
- El contexto por `context_ref` desaparece: el asesor del 104 se queda sin brief.
- El score de incomprensión pierde las señales `s3` (repregunta) y `s6` (turnos sin progreso),
  que se calculan sobre `_turnos`.
- La histéresis se pierde: `fue_derivada` vuelve a `False` y **una conversación ya derivada puede
  reentrar al flujo normal**.

Está declarado como riesgo **R-03** en `PROCEDENCIA.md` y como punto de rotura nº 2 del
dimensionamiento (`arquitectura.md` §10). Con varias réplicas el defecto no necesita ni un
reinicio: basta con que el balanceador mande el segundo turno a otra réplica.

`[CONFIRMADO-OFICIAL]` La ficha del Desafío 1 exige **«derivar a asesor humano con contexto»**.
Un contexto que se evapora al reiniciar el proceso no es contexto: es una promesa con fecha de
caducidad indeterminada.

---

## Decisión

**Se introduce LangGraph como capa de orquestación con checkpointer persistente. Los nodos
llaman a las funciones que ya existen.**

Tres compromisos concretos:

1. **El flujo de un turno se declara como grafo.** Cada rama de la tabla anterior es una arista
   condicional explícita, con su función de ruta y su `path_map`. Lo que hoy hay que leer, pasa a
   poder dibujarse y recorrerse.
2. **Cada nodo es una llamada, no una reimplementación.** `construir_hechos`,
   `evaluar_incomprension`, `generar_explicacion`, `verificar`, `auditoria.emitir` y
   `redactar_para_nivel` se invocan con la misma firma que hoy. Un nodo es, literalmente, leer
   del estado, llamar a la función que ya está probada y escribir el resultado en el estado.
3. **El estado de la conversación se persiste con `SqliteSaver`, con `thread_id = conversation_id`.**
   Deja de vivir en un `OrderedDict` del proceso. La ruta se configura con `CHECKPOINT_PATH` y por
   defecto es `data/checkpoints/turnos.sqlite` — dentro de `data/`, que ya está en `.gitignore`
   (`data/*`) y en `.dockerignore` (`data/`): **el estado de conversación de un cliente no se
   versiona ni entra en la imagen**. El valor `:memory:` fuerza el almacén volátil para pruebas.

### Tres decisiones de detalle que no son obvias

Están tomadas en `packages/orquestacion/checkpointer.py` y conviene que queden registradas,
porque las tres se podrían haber hecho de la forma cómoda y equivocada:

1. **Conexión propia con vida de proceso, no `SqliteSaver.from_conn_string`.** Ese *helper* es un
   gestor de contexto que **cierra la conexión al salir del `with`**: sirve para un guion, no para
   un servidor. Se abre `sqlite3.connect(ruta, check_same_thread=False)` y se cierra en el apagado
   ordenado. `check_same_thread=False` es correcto porque `SqliteSaver` protege cada operación con
   un `threading.Lock` propio, y `explicar_recibo` es `def` —FastAPI lo corre en el threadpool de
   anyio, un hilo distinto por petición—.
2. **Si el fichero no se puede abrir, se degrada a `InMemorySaver` y se avisa. Nunca lanza.**
   Un disco lleno o un volumen de solo lectura no pueden tumbar la explicación de un recibo: se
   pierde la persistencia entre reinicios, no la corrección. Es la misma política que ya aplica el
   retriever cuando no hay pgvector, y el `Checkpointer` publica `persistente: bool` y `motivo`
   para que «¿esta demo está persistiendo?» se responda de un vistazo.
3. **Lista blanca de tipos al deserializar.** El serializador de LangGraph **importa y construye
   la clase que diga el propio checkpoint**. Un fichero manipulado podría provocar la carga de
   clases arbitrarias. `tipos_permitidos()` restringe la reconstrucción a las clases *definidas*
   en catorce módulos del dominio —**92 tipos** en la comprobación del 7 de agosto de 2026—, y se
   deriva por introspección para que añadir un modelo nuevo no obligue a acordarse de una lista.

### Lo verificado, ejecutando el código del proyecto

`[SUPUESTO]` de partida, verificado: que un `FactSet` sobrevive el viaje al checkpointer sin
perder exactitud.

| Qué se probó | Resultado |
|---|---|
| **`abrir_checkpointer()` entre dos procesos distintos** | El proceso B (PID 10908) recuperó del fichero SQLite el estado que dejó el A (PID 20944) y **siguió acumulando sobre él**: 12345 → 24690 céntimos, enteros. `persistente=True` en ambos |
| **Round-trip de un `FactSet` real** de `C-DEMO-01` por el serializador del proyecto | El proceso B (PID 34752) lo reconstruyó **como instancia de `FactSet`**, no como diccionario —la lista blanca funciona— con `sha256` idéntico `3227801e4fcca4c4`, `total_actual_cent = 21637` y `isinstance(..., int) is True` |
| `telemetria_externa_activa()` con la configuración del proyecto | **`False`** en los dos procesos |
| Concurrencia desde el threadpool | 16 hilos simultáneos sobre un mismo `SqliteSaver`, cada uno con su `thread_id`: **0 errores en 0,932 s** |
| `interrupt()` + `Command(resume=…)` | Pausa el grafo, persiste el checkpoint, `get_state(config).next` señala el nodo pendiente y se reanuda **desde otro objeto grafo con otra conexión al mismo fichero** |
| Que no sale tráfico a LangSmith | Con `socket.socket.connect` parcheado: `tracing_is_enabled() -> False` y **cero conexiones salientes**. Control negativo con `LANGSMITH_TRACING=true`: entonces **sí** intenta salir a `34.8.121.39:443` |

El control negativo importa más que la prueba positiva: demuestra que la variable de entorno es
el interruptor real y no una suposición cómoda. Por eso
`packages/orquestacion/telemetria_externa.py` **fuerza** los valores en lugar de usar
`setdefault`, invalida la caché `lru_cache` de `langsmith.utils.get_env_var` y se importa
**antes** que cualquier símbolo de LangGraph — el orden de importación es parte del contrato y
está escrito así en `checkpointer.py:64-73`.

---

## Lo que NO cambia, y por qué

**El motor determinístico y el verificador son el 70 % del valor del proyecto. Ningún framework
de orquestación aporta nada a la aritmética.** Quedan intactos, y esa intocabilidad es parte de
la decisión, no una omisión:

| Módulo | Qué hace | Por qué no se toca |
|---|---|---|
| `packages/facts_engine/` — `tramos`, `prorrateo`, `diff`, `atribucion`, `invariante`, `confianza`, `intencion`, `motor` | Calcula el `FactSet` y cierra el invariante al céntimo | Es aritmética exacta en enteros. Un grafo no la hace más exacta; solo añade capas entre el dato y el resultado (ADR 002, ADR 004) |
| `packages/llm_layer/verificador.py` | Construye `ALLOWED` desde el `FactSet` y da el veredicto cifra por cifra | Es el garante de `TA_respuesta = 0`. Sustituirlo por cualquier mecanismo del framework sería cambiar una prueba en código por una promesa (ADR 003) |
| `packages/retriever/saneador.py` y la fusión RRF de `hibrido.py` | Deja el contexto recuperado sin un solo dígito | Es la barrera contra la alucinación más difícil de detectar: una cifra plausible de un documento real de la empresa que no es de este cliente (ADR 001) |
| `packages/governance/` | Bitácora *append-only* con cadena de hash | La cadena es la evidencia. Un orquestador que reescribiera eventos rompería `verificar_cadena()` |

**El grafo orquesta llamadas a esas funciones; no las sustituye ni las reimplementa.** Si mañana
se retira LangGraph, se retira una capa de coordinación y el motor sigue calculando igual. Esa es
exactamente la propiedad que se buscaba.

Dos asimetrías del flujo actual se declaran como invariantes del grafo, porque cambiarlas
movería la bitácora y rompería los tests de contrato:

- La rama de intención y los errores del ACL **no emiten `CHAIN`**. El grafo no debe «arreglar»
  eso unificando el cierre.
- `auditoria.emitir` **nunca** puede ir antes de un `interrupt()` dentro del mismo nodo: al
  reanudar, LangGraph **re-ejecuta el nodo desde el principio** y la bitácora encadenada
  duplicaría eventos. Los `emitir` van en nodos separados o después del `interrupt()`.

---

## Licencias — el criterio y qué se excluye

`[CONFIRMADO-OFICIAL]` BASES, sección 9:

> «Los participantes garantizan que los contenidos presentados son originales. Todo uso de
> herramientas de terceros (IA generativa, API, open source o datasets) debe cumplir
> estrictamente con sus respectivas licencias, sin vulnerar derechos de propiedad intelectual
> ajenos.»

Y en la misma sección, que la inscripción «implica la cesión de los derechos de propiedad
intelectual sobre las propuestas presentadas a favor de Integratel».

**Ese es el criterio operativo, y no es formalismo:** si la solución se cede a Integratel, cada
dependencia con licencia restringida es un peaje que Integratel tendría que negociar con un
tercero antes de poder desplegar lo que ya es suyo. Una entrega ganadora que no se puede
desplegar sin comprar una licencia es una entrega a medias.

### Lo que se usa — MIT, verificado leyendo el archivo de licencia instalado

| Paquete | Versión instalada | Licencia | Cómo se verificó |
|---|---|---|---|
| `langgraph` | 1.2.10 | **MIT** | `langgraph-1.2.10.dist-info/licenses/LICENSE` → «MIT License · Copyright (c) 2024 LangChain, Inc.» · `License-Expression: MIT` en `METADATA` |
| `langgraph-checkpoint` | 4.1.1 | **MIT** | `langgraph_checkpoint-4.1.1.dist-info/licenses/LICENSE`, texto MIT íntegro |
| `langgraph-checkpoint-sqlite` | 3.1.1 | **MIT** | `langgraph_checkpoint_sqlite-3.1.1.dist-info/licenses/LICENSE`, texto MIT íntegro |
| `langchain-core` | 1.5.3 | **MIT** | `METADATA`: `License: MIT` y `Classifier: License :: OSI Approved :: MIT License` |

Transitivas que arrastran, todas permisivas y también verificadas por metadatos:
`langgraph-prebuilt` 1.1.0 (MIT), `langgraph-sdk` 0.4.2 (MIT), `langchain-protocol` 0.0.18 (MIT),
`ormsgpack` 1.12.2 (Apache-2.0 OR MIT), `aiosqlite` 0.22.1 (MIT), `sqlite-vec` 0.1.9 (MIT/Apache-2.0),
`xxhash` 3.5.0 (BSD), `jsonpatch` 1.33 (BSD), `tenacity` 9.1.2 (Apache-2.0), `uuid-utils` 0.17.0 (BSD-3).
**Ninguna GPL ni AGPL**, en coherencia con el criterio ya declarado en
`PROCEDENCIA.md`

### Lo que se excluye deliberadamente

| Excluido | Estado en el entorno | Por qué no |
|---|---|---|
| **`langgraph-api`** y **LangGraph Platform** | **NO INSTALADO** — verificado con `importlib.metadata`, lanza `PackageNotFoundError` | `[POR VALIDAR]` Se declara bajo **Elastic License 2.0**, que no es OSI y exige clave comercial para el servicio gestionado. Como no se instala, la licencia **no se ha podido leer de un archivo local**: se toma de la declaración del proyecto y queda marcada para verificación legal si el proyecto se industrializa. La decisión no depende de ese matiz: **el paquete no aporta nada que el proyecto necesite**, y su ausencia se comprueba en un segundo |
| **`langgraph-cli`** | **NO INSTALADO** — ídem | Herramienta de despliegue de la Platform. Mismo motivo |
| **El servicio LangSmith** | El **cliente** `langsmith` 0.10.17 sí está instalado, como dependencia transitiva de `langchain-core`, y su `METADATA` declara **`License: MIT`** | **La distinción es importante y aquí se hace explícita:** lo restringido no es el cliente, es **el servicio de trazas al que apunta**, que es un producto alojado y comercial de LangChain, Inc. El paquete MIT no se puede quitar sin quitar `langchain-core`; lo que sí se puede es **no usar el servicio**, y eso es lo que se hace |

**Cómo se apaga el servicio, con precisión.** El interruptor real está en
`langsmith/utils.py:121 tracing_is_enabled()`, que resuelve por `TRACING_V2` con respaldo en
`TRACING`, sobre los espacios de nombres `LANGSMITH` y `LANGCHAIN`. Se declaran las cuatro
variables, en `.env`, `.env.example` y `docker-compose.yml`:

```dotenv
LANGSMITH_TRACING=false
LANGSMITH_TRACING_V2=false
LANGCHAIN_TRACING=false
LANGCHAIN_TRACING_V2=false
LANGSMITH_OTEL_ENABLED=false
LANGCHAIN_CALLBACKS_BACKGROUND=false
LANGSMITH_API_KEY=
LANGCHAIN_API_KEY=
LANGSMITH_ENDPOINT=
LANGCHAIN_ENDPOINT=
```

Por defecto el tracing ya está apagado; se declara explícito para que **un `.env` heredado o una
variable de CI no lo encienda por accidente**. Detalle operativo: `langsmith.utils.get_env_var`
está decorado con `@lru_cache`, así que las variables deben fijarse **antes** de importar
`langgraph` o `langchain_core`.

**Este ADR no afirma nada sobre las condiciones de tratamiento o retención de datos de ningún
proveedor.** La afirmación que sí se sostiene es de comportamiento observado y reproducible:
con esas variables, **cero conexiones salientes** hacia LangSmith.

---

## Consecuencias

**Contrapartida asumida, dicha sin adornos: una dependencia más y una capa de indirección más.**
El flujo deja de leerse de arriba abajo en un archivo y pasa a leerse como un grafo declarado;
quien retome el proyecto tiene que aprender la API de `StateGraph`. Y una traza de error puede
atravesar el `Pregel` de LangGraph antes de llegar al código propio. Es un coste real.

Lo que se compra a cambio:

- **La evidencia sobrevive al reinicio.** Es la corrección del defecto que motivó todo:
  `GET /v1/evidencia/{explicacion_id}` deja de ser una promesa condicionada a que nadie reinicie
  el contenedor.
- **El human-in-the-loop deja de ser un `context_ref` y una esperanza.** Con `interrupt()`, un
  turno puede quedar **suspendido en un nodo concreto**, con su estado persistido, esperando la
  decisión de un asesor, y reanudarse con `Command(resume=…)` **desde otro proceso o réplica**.
  Hoy la derivación abre un contexto y termina el turno; no hay forma de volver a él.
- **Las once ramas quedan declaradas.** Añadir la doceava es añadir un nodo y una arista, no
  releer 430 líneas para no romper una compuerta.
- **La reproducibilidad no cambia.** El `FactSet` sigue siendo determinista, `generado_en` sigue
  excluido del hash, y el round-trip por el checkpointer **conserva el `sha256`** (verificado).
- **Un fichero SQLite nuevo que operar.** Hay que abrirlo en el `lifespan`, cerrarlo en
  `cerrar_recursos()` y **decidir su ciclo de borrado**. Es infraestructura que antes no existía
  porque el estado se perdía solo — y es un cambio real en la superficie de datos: lo que antes
  vivía en RAM ahora está en disco. La ubicación por defecto ya queda fuera de Git y de la
  imagen; la política de retención **sigue sin definirse**.
- **SQLite es de un nodo.** Resuelve el reinicio y la evidencia; **no** resuelve por sí solo el
  multi-réplica salvo sobre volumen compartido. Para varias réplicas, el mismo grafo apunta a un
  checkpointer PostgreSQL sin tocar los nodos. Ver `PROCEDENCIA.md`.

---

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| **Seguir con la orquestación manual** | Funciona hoy y está verde: 357 pruebas, 19/19 pasos e2e, `TA_respuesta = 0`. Pero **no toca el defecto**: la memoria seguiría siendo un `OrderedDict` del proceso y `GET /v1/evidencia` seguiría devolviendo 404 tras un reinicio. Habría que escribir igualmente la persistencia y el mecanismo de suspensión-reanudación, a mano y sin pruebas de terceros detrás. Se descarta por no resolver el problema, no por falta de elegancia |
| **Dify** | `[POR VALIDAR]` Se distribuye bajo una **Apache 2.0 modificada**: según su propia licencia, el uso multi-inquilino exige acuerdo comercial y **no se permite retirar su marca de la interfaz**. Para una solución cuya PI se cede a Integratel y que viviría dentro de la App Mi Movistar y del Bot Lucía, una cláusula de atribución visual no negociable es descalificante. Añadido: es una plataforma con interfaz propia, y este proyecto es *headless* por diseño |
| **n8n** | `[POR VALIDAR]` **Sustainable Use License**, que **no está aprobada por la OSI**: restringe el uso a fines internos y limita explícitamente su reventa o su incorporación a un producto entregado a clientes finales. Movistar es exactamente ese caso. Además, un motor de workflows generalista no tipa el estado ni permite versionar el flujo junto al código en el mismo repositorio |
| **Airflow, Prefect o similares** | Son orquestadores de lotes con planificador: piensan en DAGs que corren por calendario y en minutos u horas. Aquí el turno vive **decenas de milisegundos** —mediana medida de 14 ms en modo determinístico— dentro de una petición HTTP. No es el mismo problema |
| **Máquina de estados propia + tabla en PostgreSQL** | Es la alternativa honesta y podría defenderse. Se descarta por coste de oportunidad: reimplementar checkpointing, versionado de estado, suspensión y reanudación son semanas de trabajo con sus propios fallos, y ninguna línea de eso mejora la exactitud, que es el eje del desafío. Si LangGraph estorbara, esta es la salida |
| **Sustituir el motor por agentes con herramientas** | Devolvería el cálculo a un componente no determinístico. Es precisamente lo que rechazan los ADR 003 y 004. LangGraph entra como **capa de coordinación**, nunca como sustituto del motor |

---

## Estado de la implementación

**Verificado el 7 de agosto de 2026** leyendo el árbol del proyecto y ejecutando la suite, que
sigue **en verde** (el recuento crece a diario; `make test` da el vigente). La intervención es
aditiva y va por partes.

| Pieza | Estado | Dónde |
|---|---|---|
| Apagado de la telemetría de terceros, con efecto al importar | ✅ implementado | `packages/orquestacion/telemetria_externa.py` (125 líneas) |
| Checkpointer SQLite, con degradación a memoria y lista blanca de tipos | ✅ implementado | `packages/orquestacion/checkpointer.py` (292 líneas) — `abrir_checkpointer`, `obtener_checkpointer` (`@lru_cache(maxsize=1)`), `cerrar_checkpointer`, `serializador_del_dominio`, `tipos_permitidos` |
| `EstadoTurno` y `Servicios`: qué se persiste y qué no | ✅ implementado | `packages/orquestacion/estado.py` (257 líneas) |
| Adaptador de proveedor sobre `langchain-core`, como tercer modo | ✅ implementado | `packages/llm_layer/providers/langchain_.py` (798 líneas) · `MODO_LANGCHAIN` y `MODOS_VALIDOS` en `providers/base.py:56,59` · `obtener_proveedor` en `base.py:302` · 543 líneas de pruebas en `tests/unit/test_proveedor_langchain.py` |
| **El grafo en sí** — nodos, aristas condicionales y `compile(checkpointer=…)` | ✅ implementado | `packages/orquestacion/grafo.py` y `nodos.py` — `construir_grafo`, `compilar_grafo`, `obtener_grafo`, `ejecutar_turno` |
| **Enganche con la API** — `obtener_checkpointer` en `calentar()` y en `cerrar_recursos()`, y el endpoint invocando el grafo | ✅ implementado | `apps/api/deps.py` (`_calentar_orquestacion`, `_cerrar_orquestacion`) y `apps/api/routers/explicar.py` (`_explicar_con_grafo`), bajo la variable `ORQUESTADOR` |
| Declaración de `langgraph` y sus dos paquetes de checkpoint en `pyproject.toml` | ⛔ **pendiente** | hoy solo está `langchain-core` como extra opcional `[langchain]` |

Los nombres de nodo del diagrama de `arquitectura.md` §5.1 ya son los del grafo real
(`NOMBRES_DE_NODO` en `packages/orquestacion/nodos.py`), no una propuesta.

Cómo comprobar el estado real en cualquier momento:

```bash
ls packages/orquestacion                                  # grafo.py presente = grafo escrito
grep -rn orquestacion apps/api/deps.py apps/api/routers/explicar.py   # con resultados = cableado
python -c "import importlib.metadata as m; print(m.version('langgraph'))"
python -c "import importlib.metadata as m; m.version('langgraph-api')"   # debe fallar
python -c "from packages.orquestacion.telemetria_externa import telemetria_externa_activa as t; print(t())"   # False
```

---

## Referencias

- `apps/api/routers/explicar.py:396-825` — las once ramas que el grafo declara
- `apps/api/deps.py:251-345` — `MemoriaConversaciones`, el defecto que motiva el cambio
- `apps/api/routers/evidencia.py:156-163` — el 404 tras el reinicio
- `apps/api/deps.py:348, 363, 384` — patrón de singleton, `calentar()` y `cerrar_recursos()`, donde entra el checkpointer
- `packages/orquestacion/checkpointer.py` — `Checkpointer`, degradación a memoria y lista blanca de 92 tipos
- `packages/orquestacion/estado.py` — `EstadoTurno` (se persiste) frente a `Servicios` (no se persiste)
- `packages/orquestacion/telemetria_externa.py` — `VARIABLES_APAGADO`, `VARIABLES_VACIADAS`, `telemetria_externa_activa`
- `packages/llm_layer/providers/base.py:226` — `ProveedorLLM`, protocolo `runtime_checkable` de dos miembros donde se enchufa el adaptador
- `packages/llm_layer/providers/langchain_.py` — `LangChainProvider`, el adaptador
- `docs/arquitectura.md` §5 — el grafo y el estado persistente
- `docs/PROCEDENCIA.md` §2 — declaración de licencias exigida por BASES §10
- `docs/PROCEDENCIA.md` §3.4 y riesgo R-03 — lo que queda abierto
- ADR [001](001-el-recibo-no-se-vectoriza.md), [002](002-montos-en-centimos-enteros.md),
  [003](003-el-llm-no-calcula.md), [004](004-modelo-de-tramos.md) — lo que esta decisión
  deliberadamente no toca
