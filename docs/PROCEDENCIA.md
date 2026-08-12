# Procedencia: datos, herramientas y lo que queda abierto

> **Qué es este documento.** La declaración que exigen las BASES §9 y §10: qué inteligencia
> artificial se usa, con qué licencias, sobre qué datos, y qué sigue sin resolverse. Sustituye
> a `PROCEDENCIA.md`, `PROCEDENCIA.md` y `PROCEDENCIA.md`, que decían lo
> mismo repartido en tres sitios y en 797 líneas.

**Verificado el 11 de agosto de 2026** contra el árbol de trabajo y contra la base de datos.

---

## 1. Componentes de inteligencia artificial

| # | Herramienta | Tipo | Licencia / términos | Rol exacto en la solución | ¿Procesa datos de Movistar? | Dónde se ejecuta |
|---|---|---|---|---|---|---|
| 1 | **API de Google Gemini** (`google-genai`) | Modelo generativo | SDK Apache-2.0; el servicio se rige por los términos de Google | **Redacta el texto de la explicación** a partir de un objeto de hechos previamente calculado y validado. **No calcula montos, no accede a la base de datos y no ejecuta acciones.** El identificador de modelo se lee de la variable `GEMINI_MODEL` y no está fijado en el código | Solo datos sintéticos con identificadores ficticios | Servicio en la nube |
| 2 | **API de embeddings de Gemini** | Embeddings | Ídem | Vectoriza **catálogo de conceptos, FAQs y casuísticas**. **Nunca vectoriza recibos ni importes** | No: el corpus se genera localmente y se sanea de cifras antes de indexar | Servicio en la nube |
| 3 | **`MockProvider`** (propio) | Generador determinístico | Propio | Rellena plantillas Jinja con las cifras ya validadas. Es el destino del *fallback* cuando el verificador bloquea una respuesta, y permite operar sin red | Sí, sintéticos | Local |
| 4 | **`MockEmbedder`** (propio) | Embeddings determinísticos | Propio | Permite indexar y correr las pruebas sin conexión | No | Local |
| 5 | **rank-bm25** | Recuperación léxica | Apache-2.0 | Componente léxico de la recuperación híbrida sobre FAQs | Sí, sintéticos | Local |
| 6 | **`LangChainProvider`** (propio, sobre `langchain-core`) | Adaptador de proveedor | Código propio; `langchain-core` es MIT | **No es un modelo: es la puerta.** Permite usar como redactor cualquier modelo soportado por LangChain (`LLM_MODE=langchain`) cumpliendo el mismo contrato de dos miembros que `MockProvider` y `GeminiProvider`. **El modelo que se enchufe queda sujeto a las mismas reglas: no calcula, no accede a la base de datos y su salida pasa íntegra por el verificador.** Desactivado por defecto; el modelo concreto se fija en `LLM_LANGCHAIN_MODELO` y **no hay ninguno fijado en el código** | Enviaría al modelo elegido el mismo `FactSet` sintético que ya recibe el proveedor | Local; el modelo, donde lo aloje su proveedor |

> `[POR VALIDAR]` Si se activara el modo `langchain`, el proveedor concreto que se configure **debe declararse aquí con su nombre, su licencia y sus términos vigentes** antes de cualquier uso con información real. Este documento **no afirma** nada sobre las condiciones de tratamiento ni de retención de datos de ningún proveedor.

### Herramientas de IA usadas para **construir** el proyecto, no para ejecutarlo

| # | Herramienta | Rol | ¿Forma parte del sistema en ejecución? |
|---|---|---|---|
| 7 | Asistentes de IA generativa de propósito general | Apoyo a la programación, a la redacción de documentación y al análisis de los documentos del desafío | **No.** No intervienen en ninguna respuesta al cliente ni en ningún cálculo |

---


---

## 2. Bibliotecas y plataforma

Todas de licencia permisiva. **Se excluye deliberadamente todo copyleft fuerte (GPL / AGPL)** por su incompatibilidad con la cesión de derechos de propiedad intelectual a Integratel prevista en BASES §9.

La columna **versión** indica el rango declarado en `pyproject.toml` y, entre paréntesis, la versión exactamente instalada y verificada en el entorno con el que se produjeron los resultados de `make eval` y `make test` (5 de agosto de 2026). Ninguno de estos componentes ha sido modificado: todos se instalan por gestor de paquetes.

Las cuatro filas de **LangGraph** se incorporaron el **7 de agosto de 2026** con la decisión del [`ADR/005`](ADR/005-langgraph-para-la-orquestacion.md); sus versiones instaladas se verificaron ese día con `importlib.metadata` y sus licencias, leyendo el archivo `LICENSE` de cada paquete instalado (§2.1).

**Actualización del 8 de agosto de 2026.** Las cuatro **ya están declaradas en `pyproject.toml`**, y el matiz que traía este párrafo —«instalados y verificados, pero aún no declarados»— ha dejado de ser cierto. Se declaran como **dependencias opcionales**, en dos extras separados, porque el sistema arranca sin ninguna de ellas:

```toml
[project.optional-dependencies]
langchain    = ["langchain-core>=1.5,<2.0"]
orquestacion = ["langgraph>=1.2,<2.0", "langchain-core>=1.5,<2.0",
                "langgraph-checkpoint>=4.1,<5.0", "langgraph-checkpoint-sqlite>=3.1,<4.0"]
```

Sin el extra `orquestacion`, `ORQUESTADOR=directo` conserva la vía lineal y **LangGraph ni siquiera se importa**. Sigue prohibido en ambos extras, con el comentario escrito en el propio `pyproject.toml`, todo lo de licencia restringida: `langgraph-api`, `langgraph-cli`, LangGraph Platform y el servicio LangSmith (§2.2). Comprobación en una línea: `python -c "import tomllib,pathlib; d=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); print(d['project']['optional-dependencies'])"`.

| Componente | Tipo | Versión declarada (instalada) | Licencia | Rol exacto | ¿Procesa datos de Movistar? | Dónde se ejecuta |
|---|---|---|---|---|---|---|
| Python | Lenguaje / runtime | 3.12 (3.12.1) | PSF | Todo el código del proyecto | Sí, sintéticos | Local / contenedor |
| FastAPI | Framework web | ≥0.115 (0.135.3) | MIT | Núcleo HTTP del servicio *headless* | Sí, sintéticos | Local / contenedor |
| Starlette | Framework ASGI | transitiva (1.0.0) | BSD-3 | Base de FastAPI: enrutado y middleware | Sí, sintéticos | Local / contenedor |
| Uvicorn | Servidor ASGI | ≥0.30 (0.30.6) | BSD-3 | Servidor de la API y de los mocks | Sí, sintéticos | Local / contenedor |
| Pydantic | Validación | ≥2.9 (2.12.5) | MIT | Modelo canónico y contratos de todos los esquemas | Sí, sintéticos | Local / contenedor |
| pydantic-settings | Configuración | ≥2.4 (2.4.0) | MIT | Lectura tipada del entorno y del `.env` | No | Local / contenedor |
| SQLAlchemy | Acceso a datos | ≥2.0 (2.0.35) | MIT | Consultas sobre PostgreSQL | Sí, sintéticos | Local / contenedor |
| psycopg (+ `binary`) | Conector BD | ≥3.2 (3.3.4) | LGPL-3.0 | Conector PostgreSQL. Se usa **como biblioteca, sin modificar y sin enlazado estático**, que es el supuesto que la LGPL permite sin propagar la licencia al código propio. `[POR VALIDAR con asesoría legal si el proyecto se industrializa]` | Sí, sintéticos | Local / contenedor |
| Jinja2 | Plantillas | ≥3.1 (3.1.3) | BSD-3 | Plantillas determinísticas de explicación y del prompt | Sí, sintéticos | Local / contenedor |
| PyJWT | Seguridad | ≥2.9 (2.13.0) | MIT | Emisión y validación de credenciales por nivel de aseguramiento | No: solo el `account_ref` tokenizado | Local / contenedor |
| PyYAML | Serialización | ≥6.0 (6.0.1) | MIT | Carga de `db/reglas/rules.yaml` versionado y de los casos golden | No | Local / contenedor |
| httpx | Cliente HTTP | ≥0.27 (0.27.0) | BSD-3 | Cliente del Anti-Corruption Layer hacia BrainyBill y Amdocs | Sí, sintéticos | Local / contenedor |
| rank-bm25 | Recuperación léxica | ≥0.2.2 (0.2.2) | Apache-2.0 | Componente léxico de la recuperación híbrida sobre FAQs | No: solo corpus propio saneado | Local / contenedor |
| langgraph | Orquestación | **extra opcional `[orquestacion]`**: ≥1.2,<2.0 (1.2.10) | **MIT** | **Capa de orquestación del turno**: declara como grafo las once ramas de `POST /v1/explicar` y las llama en orden. **No calcula, no verifica y no reimplementa nada del motor**: cada nodo invoca la función que ya existe. Ver [`ADR/005`](ADR/005-langgraph-para-la-orquestacion.md) | Sí, sintéticos — solo los transporta entre nodos del mismo proceso | Local / contenedor |
| langgraph-checkpoint | Persistencia de estado | **extra opcional `[orquestacion]`**: ≥4.1,<5.0 (4.1.1) | **MIT** | Interfaz de *checkpointer* y serialización del estado del grafo. Es la dependencia que hace persistente la conversación. El proyecto la usa con un serializador **restringido por lista blanca** a los tipos de su propio dominio (`packages/orquestacion/checkpointer.py`) | Sí, sintéticos | Local / contenedor |
| langgraph-checkpoint-sqlite | Persistencia de estado | **extra opcional `[orquestacion]`**: ≥3.1,<4.0 (3.1.1) | **MIT** | Implementación **SQLite** del *checkpointer*, con `thread_id = conversation_id`. Sustituye al `OrderedDict` en RAM que perdía las explicaciones al reiniciar el proceso. **Fichero local en `data/checkpoints/`, ya cubierto por `.gitignore` y `.dockerignore`; ningún servicio externo** | Sí, sintéticos: turnos, `FactSet` y contexto de derivación en un fichero local | Local / contenedor |
| langchain-core | Adaptador de proveedor | **extras opcionales `[langchain]` y `[orquestacion]`**: ≥1.5,<2.0 (1.5.3) | **MIT** | Tipos y protocolos comunes. Dependencia de `langgraph` y base del **adaptador `LangChainProvider`** (`LLM_MODE=langchain`), que expone `nombre` y `completar` para cumplir el `Protocol` `ProveedorLLM` (`packages/llm_layer/providers/base.py:226`) **sin tocar el generador, el verificador ni el motor**. Es opcional: la demo por defecto (`LLM_MODE=mock`) arranca sin ella | Enviaría al modelo elegido solo el `FactSet` sintético que ya recibía el proveedor | Local; el modelo, donde lo aloje su proveedor |
| google-genai | SDK del modelo | ≥1.0 (**no instalada**) | Apache-2.0 | SDK oficial para el proveedor Gemini. **Import diferido**: su ausencia no rompe nada y la demo corre en modo `mock` | Enviaría al servicio solo el `FactSet` sintético | Local; el servicio, en la nube |
| PostgreSQL | Base de datos | 16 | PostgreSQL License | Persistencia de corpus, hechos y bitácora | Sí, sintéticos | Contenedor `db` |
| pgvector | Extensión de BD | `pgvector/pgvector:pg16` | PostgreSQL License | Índice vectorial de catálogo, FAQs y casuísticas. **Nunca de recibos** | No | Contenedor `db` |
| Docker / Docker Compose | Empaquetado | — | Apache-2.0 | Entorno reproducible en un comando | No | Local |
| pytest · pytest-cov | Pruebas | ≥8.3 (8.3.2) · ≥5.0 | MIT | Batería de pruebas. **Solo desarrollo** | No | Local |
| Hypothesis | Pruebas de propiedad | ≥6.100 (6.165.1) | **MPL-2.0** | Prueba de propiedad del invariante. **Solo desarrollo; no se distribuye ni se enlaza en el artefacto entregado**, por lo que el copyleft débil por archivo de la MPL no alcanza a ningún código propio | No | Local |
| ruff | Estilo | ≥0.6 (0.16.1) | MIT | Análisis estático y formato. **Solo desarrollo** | No | Local |
| jsonschema | Pruebas de contrato | transitiva (4.21.1) | MIT | Validación de los esquemas JSON en `tests/contract`. Se usa con `importorskip`. **Solo desarrollo** | No | Local |

`[POR VALIDAR]` Las licencias de esta tabla se han tomado de la declaración de cada proyecto. Antes de un uso industrial conviene reejecutar un inventario automático de licencias sobre el árbol de dependencias completo, incluidas las transitivas que aquí no se enumeran una a una.

### 2.1 Las cuatro filas de LangGraph: licencias verificadas en el disco

Las cuatro son **MIT**, y no se ha tomado de una página web: se leyó el archivo de licencia del paquete instalado.

| Paquete | Versión instalada | Evidencia leída |
|---|---|---|
| `langgraph` 1.2.10 | 1.2.10 | `langgraph-1.2.10.dist-info/licenses/LICENSE` → «MIT License · Copyright (c) 2024 LangChain, Inc.» · `METADATA`: `License-Expression: MIT` |
| `langgraph-checkpoint` | 4.1.1 | `langgraph_checkpoint-4.1.1.dist-info/licenses/LICENSE`, texto MIT íntegro |
| `langgraph-checkpoint-sqlite` | 3.1.1 | `langgraph_checkpoint_sqlite-3.1.1.dist-info/licenses/LICENSE`, texto MIT íntegro |
| `langchain-core` | 1.5.3 | `METADATA`: `License: MIT` + `Classifier: License :: OSI Approved :: MIT License` |

Transitivas que arrastran, todas permisivas y verificadas por metadatos: `langgraph-prebuilt` 1.1.0 (MIT), `langgraph-sdk` 0.4.2 (MIT), `langchain-protocol` 0.0.18 (MIT), `ormsgpack` 1.12.2 (Apache-2.0 OR MIT), `aiosqlite` 0.22.1 (MIT), `sqlite-vec` 0.1.9 (MIT / Apache-2.0), `xxhash` 3.5.0 (BSD), `jsonpatch` 1.33 (BSD), `tenacity` 9.1.2 (Apache-2.0), `uuid-utils` 0.17.0 (BSD-3). **Ninguna GPL ni AGPL.**

### 2.2 Exclusiones deliberadas: `langgraph-api` y el servicio LangSmith

**El criterio** `[CONFIRMADO-OFICIAL]` sale de BASES §9, citada al inicio de este documento: la inscripción «implica la cesión de los derechos de propiedad intelectual sobre las propuestas presentadas a favor de Integratel». Si la solución se cede, **cada dependencia con licencia restringida es un peaje que Integratel tendría que negociar con un tercero para desplegar algo que ya es suyo.** Por eso se excluyen:

| Excluido | Estado verificado | Motivo |
|---|---|---|
| **`langgraph-api`** y LangGraph Platform | **NO INSTALADO.** `importlib.metadata.version('langgraph-api')` lanza `PackageNotFoundError` | `[POR VALIDAR]` Se declara bajo **Elastic License 2.0**, que no está aprobada por la OSI y condiciona el uso como servicio gestionado. Al no instalarse, esa licencia **no se ha podido leer de un archivo local**: se toma de la declaración del proyecto y queda pendiente de verificación legal. La exclusión no depende de ese matiz — el paquete no aporta nada que el proyecto necesite |
| **`langgraph-cli`** | **NO INSTALADO.** Ídem | Herramienta de despliegue de la Platform anterior |
| **El servicio LangSmith** | El **cliente** `langsmith` 0.10.17 **sí está instalado**, como dependencia transitiva de `langchain-core`, y su `METADATA` declara `License: MIT` | La distinción se hace explícita porque es fácil confundirla: **lo restringido no es el paquete cliente, que es MIT, sino el servicio alojado y comercial al que apunta**. El cliente no se puede desinstalar sin desinstalar `langchain-core`; lo que sí se hace es **no usar el servicio** |

**Cómo se apaga, y cómo se comprueba.** El interruptor real es `langsmith/utils.py:121 tracing_is_enabled()`, que lee `TRACING_V2` con respaldo en `TRACING` sobre los espacios de nombres `LANGSMITH` y `LANGCHAIN`. Se declaran explícitas en `.env`, `.env.example` y `docker-compose.yml` —no basta con confiar en el valor por defecto, porque un `.env` heredado o una variable de CI podrían encenderlo:

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

Deben fijarse **antes** de importar `langgraph` o `langchain_core`: `langsmith.utils.get_env_var` está decorado con `@lru_cache`.

**Comprobado, no supuesto.** Con `socket.socket.connect` parcheado para abortar cualquier salida: `tracing_is_enabled() -> False` y **cero conexiones salientes** durante una ejecución completa del grafo. Control negativo con `LANGSMITH_TRACING=true`: entonces **sí** intenta salir hacia `34.8.121.39:443`. El control negativo es lo que demuestra que la variable es el interruptor real.

**Este documento no afirma nada sobre las condiciones de tratamiento ni de retención de datos de ningún proveedor**, ni de LangChain, Inc. ni de ningún otro. Lo único que se afirma es un comportamiento observado y reproducible: con esa configuración, no sale tráfico.

---


---

## 3. Datos: las cuatro fuentes, y ninguna más

El sistema se alimenta de cuatro cosas. No hay una quinta.

| # | Fuente | Procedencia | Licencia / términos | Qué aporta |
|---|---|---|---|---|
| 1 | **Dataset del Desafío 1** (carpeta `desafio1`) | Entregado por la organización | Hackathon — confidencialidad de 10 años, BASES §9 | **La fuente de la demo y de la evaluación.** 297 002 líneas de cargo · 98 389 recibos · 18 471 cuentas · 732 códigos, más 20 000 cuentas de planta |
| 2 | **Datos que genera este sistema** | `packages/datagen`, semilla fija `DEMO_SEED=20260804` | Original del equipo | Recibos sintéticos con ground truth escrito en el mismo acto. Sirven para las pruebas golden, no para demostrar exactitud sobre datos reales |
| 3 | **Corpus de FAQs** | Corpus público de atención al cliente, traducido a español peruano | Permisiva (ver §3.1) | 400 preguntas frecuentes en la tabla `faq` de Supabase |
| 4 | **Catálogo de conceptos: la jerga peruana** | Transcripción de los vídeos del desafío, uso documentado y aportación del equipo | Original del equipo | 240 términos en `vocabulario_peruano`. Es lo que permite que «me tumbaron la señal» o «pásame con un pata» se entiendan |

**Ningún fichero del dataset se versiona.** `data/` está en `.gitignore` y los CSV se leen desde
su ruta original, fuera del árbol del proyecto. La confidencialidad de diez años de BASES §9 se
cumple por construcción: al repositorio viaja el esquema, nunca las filas.

No se usa **ningún dataset público de terceros**. No se ha descargado, inspeccionado ni
incorporado ninguno; el razonamiento está en la sección siguiente.

### 3.1 De dónde salen las FAQs, exactamente

No existe corpus de atención al cliente de telecomunicaciones **en español** con licencia
utilizable. El único del dominio está bajo GPL-3.0, que BASES §9 prohíbe al ceder la propiedad
intelectual a Integratel. Se partió de un corpus permisivo en inglés y se tradujo a español
peruano —«recibo», nunca «factura»; trato de usted— con el propio modelo. Cada fila traducida
queda marcada con `traducida = true`: nadie debe poder confundir una FAQ traducida con una
pregunta recogida en Perú.

### 3.2 El catálogo del Desafío 2 se retiró

Hasta el 11 de agosto, `packages/datagen/escenarios.py` conservaba el catálogo comercial
`OF001`–`OF022` transcrito del **Desafío 2**, de cuando el generador sintético se construyó
antes de que llegara el dataset de este desafío. Ya no está: los planes del generador son
ahora nombres reales del **Desafío 1**, con el importe medio medido sobre `cargo_facturado`.

| Plan | Importe medido | Apariciones en el dataset |
|---|---|---|
| Plan Ahorro Mi Movistar S/20.9 | S/ 19.80 | 667 |
| Plan Ahorro Mi Movistar S/25.9 | S/ 22.72 | 2 095 |
| Plan Mi Movistar S/29.9 | S/ 27.64 | 4 336 |
| Plan Mi Movistar S/31.9 | S/ 28.46 | 623 |

Los otros vocabularios de ese fichero —«postpago/prepago», los rangos de edad y los
veinticinco departamentos del Perú— son genéricos y públicos: no proceden de ningún fichero
entregado por Movistar.

**Del Desafío 2 no queda nada.** Las cuatro fuentes de la tabla anterior son las únicas.

## Por qué no se usa ningún dataset público

**No se usa ningún dataset público de terceros como fuente de datos de facturación.** El
único dataset del proyecto es el sintético propio, generado por `packages/datagen` con semilla
fija (`DEMO_SEED=20260804`), cuyo *ground truth* es exacto por construcción.

Esta decisión no es una preferencia: es una consecuencia de mirar qué hay disponible.

### 1.1 Qué hay realmente en los datasets públicos de telecomunicaciones

Los datasets públicos de telecomunicaciones disponibles son, casi sin excepción, **datasets de
fuga de clientes** (*churn prediction*). Su forma es siempre la misma:

- **una fila por cliente**, no por recibo ni por línea de recibo;
- **variables agregadas**: identificador de cliente, antigüedad en meses, cargo mensual, cargo
  total acumulado, tipo de contrato, servicios contratados, método de pago, y una etiqueta de
  si el cliente se fue o no;
- pensados para entrenar un clasificador binario, que es un problema **completamente distinto**
  del nuestro.

### 1.2 Qué les falta, y es exactamente lo que este proyecto necesita

| Lo que el proyecto necesita | ¿Está en un dataset público de fuga? |
|---|---|
| Líneas de recibo por concepto (renta, prorrateo, descuento, IGV…) | **No.** Hay un único importe agregado |
| Seis recibos por cliente (actual + cinco previos) | **No.** Hay una sola fila por cliente |
| Historial de órdenes (cambio de plan, suspensión, reconexión, alta de paquete) | **No.** No hay eventos, solo el estado final |
| Fechas de ciclo, emisión y vencimiento | **No** |
| Modalidad de renta (adelantada / vencida) y convención de prorrateo | **No.** Esa distinción no existe en el dominio del *churn* |
| Notas de crédito y débito, ajustes por días de suspensión | **No** |
| **Variación entre un mes y el anterior** | **No.** Y esta es la definitiva |

La ficha del Desafío 1 pide `[CONFIRMADO-OFICIAL]` *«analizar recibo actual + previos →
identificar causas más probables de variación»*, y las nueve causas oficiales del desafío
(cambio de plan, equipo financiado, compra de paquetes, cargos adicionales, promociones
vencidas, notas de crédito/débito, prorrateos, reconexiones y ajustes por días de suspensión)
son todas **diferencias entre dos documentos**.

**Con una sola fila agregada por cliente no se puede construir ni un solo `FactSet`**, porque
no hay nada que diferenciar entre meses. No es que el dato sea de peor calidad: es que el dato
que hace falta no está. Usar uno de estos datasets como fuente obligaría a inventar los seis
recibos, el desglose por concepto y el historial de órdenes; es decir, a inventar precisamente
aquello que el sistema tiene que explicar. El resultado sería un dataset **peor** que el
sintético propio —sin *ground truth* exacto— con la apariencia engañosa de ser «datos reales».

### 1.3 Y una razón de fondo

La ficha del Desafío 1 dice `[CONFIRMADO-OFICIAL]` que se compartirá una *«base sintética/
ficticia (Dummy Data) […] sin PII real»* y un *«dataset simplificado que simule la factura
actual y CINCO recibos previos, con inyección de variaciones»*. El dataset del desafío es, por
diseño, sintético. Sustituirlo por un dataset público de otro problema no aportaría realismo:
aportaría ruido.

---

---

## Parámetros por validar con Movistar

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

---

## Riesgos abiertos

Numerados para poder citarlos desde el resto de la documentación. **R-01** («el
dataset oficial no llega») y **R-02** («llega con esquema incompatible») se cerraron el 10 de agosto:
llegó, se ingirió y las 297 002 filas concilian.

| # | Riesgo | Severidad | Mitigación actual | Pendiente |
|---|---|---|---|---|
| **R-03** | **Memoria de conversación en el proceso** — **reducido.** `GET /v1/evidencia/{id}` ya sobrevive al reinicio; el historial de turnos y la histéresis de derivación, todavía no | Media | Un solo trabajador por contenedor, para no fragmentarla en silencio dentro de una réplica. El checkpointer está enganchado al ciclo de vida y el endpoint delega en el grafo ([`ADR/005`](ADR/005-langgraph-para-la-orquestacion.md)) | Leer el historial y `fue_derivada` desde el checkpoint en vez de desde RAM; índice inverso `explicacion_id → thread_id` para no depender de un barrido acotado; y checkpointer PostgreSQL para el multi-réplica —SQLite es de un nodo— |
| **R-09** | **Dependencia de un framework de terceros en el camino de una respuesta al cliente** | Baja | Los nodos solo **llaman** a funciones ya probadas: si LangGraph estorbara, se retira la capa de coordinación y el motor sigue calculando igual. Cuatro paquetes MIT, con `langgraph-api`, `langgraph-cli` y el servicio LangSmith excluidos, y el tracing apagado y comprobado con un cortafuegos de sockets | Rangos ya fijados en `pyproject.toml` (extra `[orquestacion]`). Sigue faltando el test de `tracing_is_enabled() is False` (§3.5) |
| **R-04** | **La circularidad de la evaluación** — ground truth y sistema comparten autor | Alta | Declarada en la salida de `run_eval`, en el README y en el pitch. En la demo se cede al jurado la elección del caso. La §1 de este documento es la prueba de que la advertencia no es retórica | Casos golden redactados por facturación |
| **R-05** | **Cuota o latencia del proveedor generativo durante la demo** | Media | `LLM_MODE=mock` corre sin red y es determinístico; la degradación a plantilla está implementada y anunciada con `X-Degradado`. Al menos uno de los ensayos debe hacerse en ese modo | Confirmar cuota real |
| **R-06** | **Fuga de datos de Movistar al repositorio** | Crítica | Repositorio privado; `data/` en `.gitignore` **y** en `.dockerignore`; la imagen no lleva datos. Confidencialidad de 10 años según BASES §9 | Revisión antes de cualquier publicación |
| **R-07** | **Narrativa causal engañosa** en escenarios compuestos — **cerrado el 8 de agosto de 2026** | Alta → **Baja residual** | Corregido en los cuatro frentes y protegido por regresión: `preferencia_causa` en `rules.yaml`, causas agregadas separadas por signo, `tests/golden/test_atribucion_causal.py` y los casos `G35`–`G38`, uno de ellos **inverso** para que la corrección no se pase de frenada. `precision_causa_raiz` = 100 % (391/391) sobre una verdad ya corregida | Lo residual es genérico y no tiene arreglo interno: **un escenario compuesto que el generador no imagine puede volver a producir una narrativa engañosa sin romper ninguna métrica**. Solo lo cierra el ground truth de facturación (R-04). `precision_causa_raiz` **no** entra en `InformeEvaluacion.aprobado`: decidir si debe (§1.1) |
| **R-08** | **Elegibilidad del equipo** | Fatal | Verificar que los 4 integrantes se inscribieron individualmente antes del 30 de julio, que el equipo es mixto y tiene ≥2 carreras distintas | **No tiene arreglo posterior** |


---

## 4. Tratamiento de datos

`[PROPUESTA del equipo]`

1. **El componente generativo recibe únicamente un objeto de hechos ya calculado y validado** por el motor determinístico. No tiene acceso a la base de datos, no ejecuta acciones y no realiza operaciones aritméticas.
2. **Todas las cifras del texto final se inyectan por sustitución desde ese objeto** y se verifican después contra él en código, quedando registro en la bitácora de auditoría.
3. **Los datos procesados son sintéticos**, con identificadores ficticios y sin información personal, conforme a la base ficticia prevista para la Hackathon.
4. **El acceso al modelo está encapsulado tras una interfaz de proveedor intercambiable**, con una implementación local determinística que permite operar sin conexión.
5. **En un despliegue productivo**, esta interfaz permitiría apuntar a un endpoint empresarial bajo los acuerdos de tratamiento de datos que Integratel defina, sin modificar el resto de la arquitectura.

> **`[POR VALIDAR]`** Las condiciones de tratamiento y retención de datos del proveedor del modelo **deben verificarse en sus términos vigentes** antes de cualquier uso con información real. Este documento **no afirma** nada al respecto.

---

## 5. Repositorio y propiedad intelectual

`[CONFIRMADO-OFICIAL]` BASES §9: la información proporcionada por Movistar «tendrá carácter confidencial y no podrá ser divulgada […] durante 10 años posterior a la finalización de la Hackathon», y la inscripción «implica la cesión de los derechos de propiedad intelectual sobre las propuestas presentadas a favor de Integratel […] reconociendo la autoría de los equipos».

Medidas adoptadas `[PROPUESTA]`:

- **Repositorio privado desde el primer commit.**
- `data/` está en `.gitignore`. Ningún archivo proporcionado por Movistar se versiona, **ni siquiera el ficticio**. Del Desafío 2 no queda nada en el árbol de trabajo: el catálogo comercial que se le había tomado se sustituyó por los nombres reales del Desafío 1 (§3.2).
- Todo el código es original del equipo. No se ha incorporado ningún repositorio de terceros más allá de las bibliotecas declaradas en la sección 2, todas instaladas por gestor de paquetes y sin modificar.
- Se descarta cualquier dependencia con licencia copyleft fuerte o no comercial.
- El modelo generativo se consume por API; **no se redistribuye ningún peso de modelo**.

---

## 6. Resumen en una línea

> Un motor determinístico propio calcula; un modelo de lenguaje de terceros redacta lo que el motor ya calculó; y un verificador en código comprueba, cifra por cifra, que el modelo no inventó nada.