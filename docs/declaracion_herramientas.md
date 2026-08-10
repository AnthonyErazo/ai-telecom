# Declaración de herramientas, APIs, datasets y servicios de terceros

**Obligación de origen** `[CONFIRMADO-OFICIAL]` — BASES, sección 10 «Uso de herramientas de IA generativa»:

> «Se permite el uso de herramientas de desarrollo, plataformas low-code/no-code y herramientas de inteligencia artificial generativa. El uso de inteligencia artificial deberá ser declarado, especificando las herramientas utilizadas y su rol en la solución. El uso de datasets, API o servicios de terceros deberá ser declarado.»

Y BASES, sección 9:

> «Los participantes garantizan que los contenidos presentados son originales. Todo uso de herramientas de terceros (IA generativa, API, open source o datasets) debe cumplir estrictamente con sus respectivas licencias, sin vulnerar derechos de propiedad intelectual ajenos.»

Todo lo que sigue es la declaración del equipo `[PROPUESTA]`, salvo las citas marcadas.

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

## 3. Datasets

| Dataset | Origen | Licencia | Contenido | Uso |
|---|---|---|---|---|
| **Dataset sintético propio** | Generado por `packages/datagen` con semilla fija (`DEMO_SEED=20260804`) | Original del equipo; ninguna licencia de terceros | 300 cuentas ficticias, recibo actual + 5 previos, historial de órdenes, catálogo de conceptos, FAQs y casuísticas | **Único dataset usado para calcular, demostrar y evaluar.** No contiene ningún dato personal ni real. Identificadores del tipo `C-00001`, sin DNI ni teléfono |
| **CSV de ejemplo del adaptador de ensayo** (`data/ejemplos_externos/telco_ficticio.csv`) | **Inventado a mano por el equipo.** No se descargó de ninguna parte | Original del equipo; ninguna licencia de terceros | 15 filas con el **esquema típico** de los datasets públicos de fuga de clientes (identificador, antigüedad, cargo mensual, cargo total, contrato, método de pago, servicios) | Permite probar `packages/datagen/mapping/kaggle_map.py` **sin conexión y sin cuenta en ninguna plataforma**. Único archivo versionado bajo `data/` junto al `README.md`. **No alimenta la demo ni la evaluación** |
| **Datasets públicos de telecomunicaciones** (p. ej. los de fuga de clientes) | Repositorios públicos de terceros | — | — | **NO SE USA NINGUNO.** No se ha descargado, inspeccionado ni incorporado ninguno. Razón técnica: tienen **una fila agregada por cliente** y carecen de líneas de recibo, de seis recibos por cliente y de historial de órdenes, por lo que **no permiten construir un solo `FactSet`**. Razonamiento completo en [`docs/datasets_externos.md`](datasets_externos.md) |
| **Dataset del Desafío 2 entregado por Movistar** (`dataset_clientes`, `catalogo_ofertas_entrega`, `historial_campanias`, `diccionario_datos_participantes`) | **Entregado por la organización.** Es de otro desafío, pero es lo único real que se ha recibido | Términos de la Hackathon — **confidencialidad de 10 años, BASES §9** | 100 000 clientes ficticios · 22 ofertas con nombre comercial y precio · 300 112 ofrecimientos · el diccionario oficial campo por campo | **Se usa su vocabulario y su catálogo; no sus datos.** Del catálogo se adoptan los **nombres comerciales y los precios** —la propia ficha los declara ficticios y creados para la hackatón— y del `dataset_clientes` los **valores admisibles** de `tipo_cliente`, `edad_rango`, `ubicacion_departamento` y `canal_mas_usado`, para que el generador sintético hable el idioma real de Movistar. **Ninguna fila, ningún identificador de cliente y ningún fichero se han incorporado al repositorio**; los CSV y XLSX se leen desde su ruta original, fuera del árbol del proyecto. Ver §3.1 |
| **Dataset oficial del Desafío 1** | Pendiente de entrega por la organización | Términos de la Hackathon (BASES §9) | — | **Aún no recibido.** Cuando llegue se integrará exclusivamente a través de `packages/datagen/mapping/movistar_map.py`. Quedará sujeto a la obligación de confidencialidad de 10 años de BASES §9 y **no se incorporará al repositorio** |

### 3.1 El dataset del Desafío 2: qué se tomó y qué se dejó fuera

`[CONFIRMADO-OFICIAL]` BASES §9: la información proporcionada por Movistar «tendrá carácter confidencial y no podrá ser divulgada […] durante 10 años posterior a la finalización de la Hackathon». **Esa obligación se aplica aquí en su forma más estricta: ningún fichero entregado por Movistar entra en el repositorio, ni siquiera para pruebas, ni siquiera recortado.**

La regla operativa que se siguió, y que debe seguir cualquiera que retome el proyecto:

| | Qué |
|---|---|
| **Se toma** | El **vocabulario**: los valores admisibles de `tipo_cliente` (postpago, prepago), `edad_rango`, `ubicacion_departamento` y `canal_mas_usado`. Y el **catálogo comercial**: los 22 nombres de oferta `OF001`–`OF022` con su precio mensual |
| **Por qué se puede** | La ficha del desafío declara ese catálogo **ficticio y creado para la hackatón**. Un nombre de plan y su tarifa de lista no son información confidencial de cliente; son el idioma en el que un recibo de Movistar está escrito, y sin él la explicación suena a laboratorio |
| **No se toma** | Ninguna fila de `dataset_clientes.csv` ni de `historial_campanias.csv`; ningún `cliente_id` del tipo `CLI000001`; ningún campo de comportamiento (consumo, mora, reclamos, contactabilidad, medio probatorio) |
| **No se copia** | **Ningún fichero.** Ni CSV, ni XLSX, ni el DOCX del diccionario. Cuando hubo que leerlos se leyeron **desde su ruta original**, fuera del árbol del proyecto |

**Comprobado, no supuesto** (8 de agosto de 2026). Se comparó por **md5** cada uno de los 514 ficheros del repositorio contra los 8 del dataset —**cero coincidencias**— y se buscó por **contenido**: las cabeceras literales de los tres CSV y el patrón `CLI\d{6}` de identificador de cliente. Ninguna aparece en ningún fichero del repositorio. Las únicas apariciones de la cadena `OF0nn` son referencias a los códigos de oferta en tres ficheros de documentación y en el comentario de procedencia de `packages/datagen/escenarios.py`, que es exactamente lo que esta declaración describe.

Donde se ve el resultado: `packages/datagen/escenarios.py` (constantes `PLANES_MOVIL`, `PLANES_HOGAR`, `PLANES_TV`, `PLANES_TOTAL`, `PAQUETES_*`, con el precio en céntimos enteros y el código `OFnnn` en el comentario) y `packages/datagen/generar.py` (`_perfil_comercial`). Los 300 recibos sintéticos usan **13 de las 22 ofertas** y **ningún nombre de plan fuera del catálogo**.

**Adaptador de ensayo `packages/datagen/mapping/kaggle_map.py`** `[PROPUESTA]` — ingiere el esquema tabular típico de los datasets públicos de fuga y sintetiza cuentas canónicas para demostrar que el *anti-corruption layer* funciona contra un esquema ajeno. **Advertencia metodológica declarada:** los recibos que produce son **parcialmente sintéticos** (el cargo mensual y la antigüedad vendrían del dataset; el desglose por concepto, las fechas de ciclo y los movimientos los sintetiza el equipo) y por tanto **sirven para ejercitar la ingesta, no para validar la exactitud del motor**. Cada cuenta producida lleva su procedencia campo por campo (`DATASET_EXTERNO` / `DERIVADO_DEL_DATASET` / `SINTETIZADO_POR_EL_EQUIPO`).

**Compromiso si algún día se usara un dataset de terceros** `[PROPUESTA]` — se declararía aquí con nombre, autor, URL, versión y **licencia literal**; se verificaría que permite uso comercial y obras derivadas (BASES §9 prevé la cesión de derechos de PI a Integratel, incompatible con licencias no comerciales); se comprobaría la ausencia de datos personales; no se versionaría; y sus datos no entrarían en ninguna métrica de exactitud. La lista de comprobación completa está en [`docs/datasets_externos.md`](datasets_externos.md) §4.2.

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
- `data/` está en `.gitignore`. Ningún archivo proporcionado por Movistar se versiona, **ni siquiera el ficticio**. Del dataset del Desafío 2 se tomaron **solo el vocabulario y el catálogo comercial**, y la ausencia de cualquier fichero suyo en el árbol se verificó por md5 y por contenido (§3.1).
- Todo el código es original del equipo. No se ha incorporado ningún repositorio de terceros más allá de las bibliotecas declaradas en la sección 2, todas instaladas por gestor de paquetes y sin modificar.
- Se descarta cualquier dependencia con licencia copyleft fuerte o no comercial.
- El modelo generativo se consume por API; **no se redistribuye ningún peso de modelo**.

---

## 6. Resumen en una línea

> Un motor determinístico propio calcula; un modelo de lenguaje de terceros redacta lo que el motor ya calculó; y un verificador en código comprueba, cifra por cifra, que el modelo no inventó nada.
