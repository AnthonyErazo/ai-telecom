# Cómo probar recibo-claro

Manual de pruebas para alguien que acaba de clonar el repositorio y no ha leído nada más.
Responde a seis preguntas: qué es, qué necesita, cómo se levanta, qué se prueba, qué respuesta
debe esperar y qué hacer cuando algo falla.

**Todas las cifras y todas las salidas de este documento se obtuvieron ejecutando el sistema el
5 de agosto de 2026** en Windows 11 con Python 3.12.1. Lo que no se pudo ejecutar está marcado
como `[NO VERIFICADO]` de forma explícita.

> **Revisión de calidad del 5 de agosto de 2026.** Se recorrió el manual comando por comando
> contra una instalación limpia. Lo que se corrigió con la medición delante: el conteo de eventos
> de la bitácora por turno (son **11**, no 10), el número de rutas del contrato (**16**, no 20),
> el resumen de la suite (**372 pasan de 434**, no 357 de 419), la promesa de la cabecera
> `X-Degradado` (no aparece cuando el proveedor ni siquiera llega a construirse — ver el matiz en
> la [sección 4.4](#44-cómo-comprobar-que-está-usando-gemini-de-verdad-y-no-el-mock)), el ejemplo
> de `periodo` inválido (sin `utterance` devuelve `200`, no `404`) y el aviso, ahora en el sitio
> donde hace falta, de dar bitácora propia a toda instancia secundaria. Se añadió la
> [sección 9.7](#97-la-ingesta-de-un-dataset-externo--kaggle_map) y el estado real de `ruff`.

Índice:

1. [Qué es esto, en 5 líneas](#1-qué-es-esto-en-5-líneas)
2. [Qué necesita](#2-qué-necesita)
3. [Ruta A — la más rápida: sin Docker y sin claves](#3-ruta-a--la-más-rápida-sin-docker-y-sin-claves)
4. [Ruta B — con la clave de Gemini](#4-ruta-b--con-la-clave-de-gemini)
5. [Ruta C — Docker completo con PostgreSQL y pgvector](#5-ruta-c--docker-completo-con-postgresql-y-pgvector)
6. [El recorrido de prueba: los tres clientes de guion](#6-el-recorrido-de-prueba-los-tres-clientes-de-guion)
7. [Las tres pruebas que hay que hacer sí o sí](#7-las-tres-pruebas-que-hay-que-hacer-sí-o-sí)
8. [Cómo leer la terminal de gobernanza](#8-cómo-leer-la-terminal-de-gobernanza)
9. [Los comandos de verificación](#9-los-comandos-de-verificación)
10. [Problemas frecuentes](#10-problemas-frecuentes)
11. [Qué NO hace](#11-qué-no-hace)

**Si tiene prisa:** `pip install -e ".[dev]"` · `python scripts/dev.py` ·
`python scripts/probar_e2e.py`. Tres comandos, dos minutos, y termina con la consola abierta y
`19/19 pasos` en verde.

---

## 1. Qué es esto, en 5 líneas

1. Un asistente que le explica a un cliente de Movistar **por qué su recibo cambió de un mes a
   otro**, con lenguaje humano y con la aritmética cuadrada al céntimo.
2. El cálculo lo hace un **motor determinístico en Python** que produce un `FactSet`: un objeto
   sellado con SHA-256 donde cada monto es un entero en céntimos.
3. El modelo de lenguaje **solo redacta**. No suma, no resta, no prorratea y no inventa: recibe
   los hechos ya calculados y los pone en castellano.
4. Antes de entregar la respuesta, un **verificador numérico** extrae todas las cifras del texto
   y comprueba una por una que estén en el `FactSet` o se deriven de él por álgebra permitida.
5. Si aparece una sola cifra sin respaldo, la respuesta **no se entrega**: se sustituye y el
   turno se deriva a un asesor humano. Esa es la métrica comprometida: `TA_respuesta = 0`.

### El flujo, de la pregunta a la respuesta

```
   Cliente: "¿por qué me vino más caro este mes?"
        │
        v
 [1] MOTOR DE HECHOS  (packages/facts_engine)          <-- ARITMETICA, sin modelo
     lee el recibo actual + los 5 previos (BrainyBill)
     y el historial de ordenes (Amdocs), y construye el FactSet:
        total_previo_cent 19555 · total_actual_cent 21637 · delta +2082
        4 lineas, causas atribuidas, invariante residual = 0 c
        sellado con sha256  3227801e4fcc...
        │
        v
 [2] RECUPERACION DE CONOCIMIENTO  (packages/retriever) <-- BM25 + vectorial
     trae definiciones del catalogo, FAQs y casuisticas.
     OJO: el recibo NUNCA se vectoriza. Solo se recupera lenguaje,
     nunca cifras. (ver docs/ADR/001-el-recibo-no-se-vectoriza.md)
        │
        v
 [3] REDACCION  (packages/llm_layer)                    <-- aqui, y solo aqui, el LLM
     recibe el FactSet + el contexto y devuelve JSON estructurado.
     Si no hay modelo disponible, una plantilla deterministica
     escribe exactamente las mismas cifras.
        │
        v
 [4] VERIFICADOR NUMERICO  (packages/llm_layer/verificador.py)  <-- CODIGO, no modelo
     extrae TODAS las cifras del texto -> las normaliza a tokens
     (cent:2082, num:31, pct:8473, fecha:2026-08-13)
     -> compara contra el conjunto ALLOWED construido del FactSet
        ANCLADA   la cifra esta literal en el FactSet
        DERIVADA  sale del FactSet por algebra registrada (suma, resta, dias/D, %)
        NO ANCLADA  no sale de ningun sitio  ->  VEREDICTO FAIL
        │
        ├── PASS  -> se entrega la respuesta con sus citas y su evidencia
        └── FAIL  -> se reintenta una vez; si vuelve a fallar, NO se entrega:
                     un aviso sin cifras + derivacion a un asesor humano
        │
        v
 [5] RESPUESTA + EVIDENCIA + BITACORA
     bloques tipados (texto, kv, puente, tabla, aviso), acciones,
     gobernanza (veredicto, citas, aserciones) y un log encadenado
     por SHA-256 que se puede auditar despues.
```

**Lo esencial: el modelo de lenguaje no calcula.** Se puede apagar por completo y el recibo se
sigue explicando con los mismos números — eso se prueba en la
[sección 7.b](#7b-apague-el-modelo-y-compruebe-que-las-cifras-no-cambian).

---

## 2. Qué necesita

| Qué | ¿Obligatorio? | Para qué | Si no lo tiene |
|---|---|---|---|
| **Python 3.12** (`>=3.12,<3.13`) | **SÍ** | Ejecutar la API, la consola, las pruebas y la evaluación | No hay ruta sin Python salvo Docker (ruta C) |
| Dependencias del proyecto (`pip install -e ".[dev]"`) | **SÍ** | FastAPI, pydantic, httpx, pyjwt, rank-bm25, pytest | Nada arranca |
| El dataset sintético (`data/sintetico`) | **SÍ para datos de cuenta** | Los recibos y las órdenes de los 300 clientes | `scripts/dev.py` lo genera solo si falta. Sin él, `/salud` y `/v1/catalogo` responden igual, pero `/v1/hechos` devuelve `404 CUENTA_NO_ENCONTRADA` |
| **Clave de Gemini** (`GEMINI_API_KEY`) | **NO. Opcional** | Redacción con un modelo real en vez del proveedor determinístico | Todo funciona en modo `mock`. Ver el recuadro de abajo |
| **Docker** | **NO. Opcional** | Ruta C: PostgreSQL 16 + pgvector, los dos mocks HTTP y la imagen de despliegue | El índice vectorial vive en memoria del proceso y el sistema responde exactamente lo mismo |
| **PostgreSQL** | **NO** | Persistir el índice vectorial del RAG entre procesos | Se degrada a memoria anunciándolo en el log y en `/salud/preparacion` |
| **Internet** | **NO** | Solo para Gemini. La consola `/ui` funciona sin red: no pide un solo recurso externo | El sistema completo corre sin red |
| `make` | **NO** | Atajos del `Makefile` | En Windows normalmente no está. Este documento da el comando crudo equivalente de cada objetivo |
| `curl` y `jq` | **NO** | Comodidad | Todos los ejemplos tienen su equivalente en Python o PowerShell |
| Navegador | Recomendado | La consola de demostración en `/ui` | Todo se puede hacer por línea de comandos |

> ### El modo `mock` no es una versión degradada de mentira
>
> Es importante entenderlo antes de probar: `LLM_MODE=mock` **no** es un simulacro para que la
> demo no se caiga. Es el **camino determinístico real** del sistema, el mismo que actúa como
> respaldo del verificador en producción.
>
> Cuando el verificador marca `FAIL`, o cuando Gemini se cae, o cuando la latencia se agota, la
> respuesta la escribe esta misma capa de plantillas determinísticas. Es decir: **el camino que
> usted prueba en modo mock es exactamente el que protege al cliente cuando el modelo falla.**
> Sus cifras salen del `FactSet` por construcción, y por eso ese camino pasa la verificación
> siempre.
>
> Verificado: con el modelo completamente ausente, `C-DEMO-01` responde `PASS · 12 ancladas ·
> 0 no ancladas` y el mismo `factset_sha256`. La única diferencia es una palabra de redacción.

---

## 3. Ruta A — la más rápida: sin Docker y sin claves

Tiempo: unos 3 minutos. Requisitos: Python 3.12 y nada más. Sin red, sin base de datos y sin
ninguna variable de entorno.

### Paso 1 — instalar el proyecto

```bash
cd recibo-claro
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Equivale a `make instalar`.

**Qué debería ver:** la instalación de FastAPI, uvicorn, pydantic, SQLAlchemy, psycopg, jinja2,
rank-bm25, google-genai, pyjwt, pyyaml, httpx, más pytest, hypothesis y ruff, y al final:

```
Successfully installed ... recibo-claro-0.1.0 ...
```

Compruebe la versión del intérprete:

```bash
python --version
```

```
Python 3.12.1
```

Si sale 3.13 o superior la instalación falla: `pyproject.toml` declara
`requires-python = ">=3.12,<3.13"`.

### Paso 2 — arrancar, con un solo comando

Desde la **raíz del repositorio** (importante: el intérprete resuelve `apps.api.main` desde el
directorio actual):

```bash
python scripts/dev.py
```

Equivale a `make dev`. Este guion hace tres cosas: **genera el dataset si falta**, fuerza
`MODO_ALMACENAMIENTO=memoria` (así no intenta hablar con ninguna base de datos), levanta uvicorn
con recarga automática y **abre la consola en el navegador**.

**Qué debería ver** (salida real):

```
recibo-claro · arranque local (sin Docker, sin PostgreSQL)
  almacenamiento MODO_ALMACENAMIENTO=memoria
  dataset      ya está en ...\recibo-claro\data\sintetico (300 cuentas)
  API          http://127.0.0.1:8000/docs
  interfaz     http://127.0.0.1:8000/ui
  comprobar    python scripts/probar_e2e.py --api http://127.0.0.1:8000

router /dev montado: emite tokens de prueba. ENTORNO=dev
INFO:     Started server process [22628]
INFO:     Waiting for application startup.
INFO apps.api.main | arrancando recibo-claro con {'entorno': 'dev', 'llm_mode': 'mock', 'almacenamiento': 'memoria→memoria', 'rules_version': '1.0.0', 'verificador_estricto': True, 'brainybill': 'archivo:...\data\sintetico', 'amdocs': 'archivo:...\data\sintetico', 'jwt_secreto_por_defecto': True, 'cors': ['*']}
INFO apps.api.deps | almacenamiento en memoria (MODO_ALMACENAMIENTO=memoria): no se usa PostgreSQL. El índice RAG vive en el proceso; el dataset y la bitácora, en disco.
INFO packages.retriever.vectorial | índice vectorial EN MEMORIA (MODO_ALMACENAMIENTO=memoria): no se abre ninguna conexión
INFO packages.retriever.corpus | FAQ: 36 documentos desde ...\data\sintetico\faqs.json
INFO packages.retriever.corpus | casuísticas: 22 desde ...\data\sintetico\casuisticas.json + 6 de la semilla para firmas no cubiertas
INFO packages.retriever.vectorial | índice vectorial: 95 vectorizados, 0 sin cambios (modelo mock:768)
INFO apps.api.deps | proveedor generativo activo: mock
INFO apps.api.acl | ACL configurado: BrainyBill=archivo:...\data\sintetico · Amdocs=archivo:...\data\sintetico
INFO apps.api.main | dependencias listas: {...}
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Las cuatro líneas que confirman que va bien: `dataset ya está en …`,
`proveedor generativo activo: mock`, `índice vectorial: 95 vectorizados` y
`Application startup complete`. Que el índice esté en memoria **no es un error**: es la ruta A
funcionando como debe.

Opciones útiles: `--puerto 8010`, `--sin-navegador`, `--sin-recarga`, `--puerto-fijo` (por
defecto, si el puerto está ocupado busca otro y lo avisa).

Si prefiere arrancar a mano, sin el guion:

```bash
python -m packages.datagen.generar --seed 20260804 --clientes 300 --periodo-actual 2026-07 --salida data/sintetico
python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000
```

Del dataset generado, compruebe `data/sintetico/resumen.json` (contenido real):

```json
{"seed": 20260804, "periodo_actual": "2026-07", "rules_version": "1.0.0",
 "clientes": 300, "recibos": 1800, "ordenes": 432, "filas_ground_truth": 835,
 "por_escenario": {"ALTA_PAQUETE": 49, "CAMBIO_PLAN_MEDIO_CICLO": 60, "CORTE_RECONEXION": 45,
                   "CUOTA_EQUIPO_FINANCIADO": 56, "DEUDA_ANTERIOR": 53, "ESTABLE": 30,
                   "FIN_DESCUENTO": 46, "NOTA_CREDITO": 52},
 "por_modalidad": {"ADELANTADA": 153, "VENCIDA": 147},
 "conceptos_catalogo": 31, "faqs": 36, "casuisticas": 22}
```

Es determinístico: misma semilla, mismos bytes.

### Paso 3 — comprobar que está viva

En **otra** terminal:

```bash
curl -s http://127.0.0.1:8000/salud
```

PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/salud
```

**Qué debería ver:**

```json
{"estado":"ok","servicio":"recibo-claro-api","entorno":"dev","rules_version":"1.0.0",
 "llm_mode":"mock","verificador_estricto":true,"en_pie_s":42.6}
```

Y la sonda de preparación, que dice de qué depende de verdad el proceso:

```bash
curl -s http://127.0.0.1:8000/salud/preparacion
```

**Qué debería ver** (salida real con `scripts/dev.py`):

```json
{
 "almacenamiento": {"modo": "memoria", "previsto": "memoria", "dsn_definido": false,
                    "destino": "memoria", "motivo": "MODO_ALMACENAMIENTO=memoria"},
 "rag": {"corpus": {"concepto_catalogo": 31, "faq": 36, "casuistica": 28},
         "bm25": {"documentos": 36, "implementacion": "rank_bm25"},
         "vectorial": {"respaldo": "memoria", "modelo": "mock:768", "dimension": 768,
                       "documentos": 95, "motivo_degradacion": "MODO_ALMACENAMIENTO=memoria"},
         "casuisticas_indexadas": 28},
 "llm": {"modo": "mock", "proveedor": "mock", "degradado": false},
 "auditoria": {"ruta": "...\\data\\auditoria\\eventos.jsonl", "cadena_valida": true,
               "indice_roto": null},
 "estado": "ok", "listo": true
}
```

Las tres que importan: **`"listo": true`**, **`"cadena_valida": true`** y
**`"llm": {..., "degradado": false}`**.

### Paso 4 — la prueba de extremo a extremo

Este es el guion que decide si el despliegue es válido. Solo usa la biblioteca estándar: no
necesita `curl`, ni `jq`, ni Docker, ni base de datos.

```bash
python scripts/probar_e2e.py --api http://127.0.0.1:8000
```

Equivale a `make probar`.

**Qué debería ver** (salida real, completa):

```
==============================================================================
PRUEBA END-TO-END · recibo-claro · C-DEMO-01 · 2026-07
API http://127.0.0.1:8000 · sin Docker · sin PostgreSQL
==============================================================================
  [PASA ] salud                              entorno=dev · llm=mock · reglas=1.0.0
  [PASA ] preparacion (sin PostgreSQL)       almacenamiento=memoria→memoria · catálogo=31 faq=36 casuística=28 · listo=True
  [PASA ] sistemas externos (ACL)            BrainyBill=TransporteArchivo · alcanzable=True
  [PASA ] token LOA2                         cuenta=C-DEMO-01 · 331 bytes
  [PASA ] hechos conciliados                 Δ=2082 c · residual=0 c · líneas=4 · sha256=3227801e4fcc
  [PASA ] explicar CORTO                     PASS · 12/12 ancladas · 5 bloques · modo=LLM · trace=tr-6d4ba6e486b0
  [PASA ] mismo FactSet sellado              explicar=3227801e4fcc · hechos=3227801e4fcc
  [PASA ] explicar DETALLE                   PASS · 30/30 ancladas · bloques=texto,kv,puente,texto,texto,tabla,texto
  [PASA ] evidencia del turno                24 items · tipos=casuistica,cat,factset,faq,linea,mov,tramo · saneado=True
  [PASA ] derivación a asesor                context_ref=ctx-99efeaeda32e5ed1 · cola=FACTURACION_104 · brief=7 líneas · vigencia=120 min
  [PASA ] contexto recuperable (104)         HTTP 200 · claves=12 · cuenta=C-DEMO-01
  [PASA ] auditoría del turno                11 eventos · veredicto=PASS · cadena_valida=True
  [PASA ] cadena de hashes (JSONL local)     2114 eventos · íntegra=True · último=235381e9021d
  [PASA ] LOA1 sin importes                  0 dígitos en 869 caracteres · redactado_por_nivel=LOA1
  [PASA ] LOA0 bloqueado en /v1/hechos       HTTP 403 · codigo=NIVEL_INSUFICIENTE · nivel_requerido=LOA2
  [PASA ] sin token → 401                    HTTP 401 · codigo=TOKEN_AUSENTE
  [PASA ] modo adversario caza la cifra      limpio=PASS · envenenado=FAIL · infractores=['S/ 28.13']
  [PASA ] turno adversario → derivación      veredicto=FAIL · motivo=VERIFICACION_FALLIDA · context_ref=ctx-b75bf16094f652d0 · dígitos entregados=0
  [PASA ] modo adversario desactivado        PASS · 0 sin anclar

==============================================================================
TODO PASA · 19/19 pasos en 1.98 s
Explicación verificada, cero cifras sin anclar, cadena de hashes íntegra
y el caso adversario bloqueado. Sin base de datos de por medio.
==============================================================================
```

Código de salida `0`. `1` = algún paso falló (el informe dice cuál y por qué); `2` = la API no
respondió. Los `trace_id`, los `context_ref`, el número de eventos de la bitácora y los
milisegundos cambian en cada ejecución; **todo lo demás no**: `12/12 ancladas`,
`Δ=2082 c`, `residual=0 c` y `sha256=3227801e4fcc` son constantes del dataset determinístico.

Opciones: `--cuenta C-DEMO-02`, `--json` (informe para CI), `--esperar 60` (espera a que la API
arranque).

### Paso 5 — la consola de demostración

Abra en el navegador:

```
http://127.0.0.1:8000/ui
```

(`scripts/dev.py` ya la abrió por usted salvo que pasara `--sin-navegador`.)

**Qué debería ver:** una consola a dos columnas titulada *"recibo-claro · consola de
demostración"*:

- **Columna izquierda, el cliente.** Un selector con los tres clientes de guion
  (`C-DEMO-01` cambio de plan · `C-DEMO-02` corte y reconexión · `C-DEMO-03` fin de descuento
  más deuda), un conmutador `Corto` / `Detalle`, la ficha del recibo —que sale de
  `GET /v1/hechos`, no del texto— y una conversación donde se pintan los bloques tipados. Trae
  tres preguntas rápidas: *"¿Por qué me vino más caro?"*, *"¿Qué me están cobrando?"* y
  *"Quiero hablar con un asesor"*.
- **Columna derecha, la gobernanza.** El contador grande
  `AFIRMACIONES NUMÉRICAS — · ANCLADAS — · NO ANCLADAS —`, los indicadores del turno, el botón
  rojo **Inyectar alucinación**, el indicador **Modo del modelo** (`mock` / `gemini`, leído de
  `GET /salud`) y la bitácora encadenada con las seis líneas del pipeline y el hash de la cadena.
- **Panel desplegable de evidencia**, *"De dónde salió esta cifra"*: se abre al pulsar sobre un
  importe y muestra de qué línea, movimiento o tramo del recibo salió.

La consola pide su propio token a `POST /dev/token` (nivel LOA2, canal APP), así que no hay que
configurar nada. Solo funciona con `ENTORNO=dev`, que es el valor por defecto.

Dos notas honestas:

- La consola **no pide un solo recurso a internet** (el favicon va embebido y el CSS y el JS son
  locales): funciona en una sala sin red. Verificado: `/ui/` y sus ocho archivos
  (`estilos.css`, `app.js`, `api.js`, `bloques.js`, `gobernanza.js`, `hechos.js`, `puente.js`,
  `formato.js`) se sirven con `200`, y todos los endpoints que consume responden correctamente.
  `[NO VERIFICADO]` la apariencia: no se pudo abrir un navegador desde el entorno donde se
  redactó este manual.
- Es una **consola de demostración**, no la App de Movistar ni el Bot Lucía. Ver la
  [sección 11](#11-qué-no-hace).

También está la documentación interactiva del contrato en `http://127.0.0.1:8000/docs`
(Swagger UI, verificado `200`) con las **16 rutas** del contrato (contadas en `/openapi.json`:
16 rutas, 16 operaciones). Si mira `app.routes` verá 21 objetos: esas 16 más `/openapi.json`,
`/docs`, `/docs/oauth2-redirect`, `/redoc` —que las añade FastAPI— y el `Mount` de `/ui`, que no
es una ruta de API y por eso no aparece en el contrato. Ahí sí hay una dependencia externa: Swagger se
carga desde `cdn.jsdelivr.net`, así que sin internet en el navegador esa página sale en blanco.
En ese caso, el contrato completo está en `http://127.0.0.1:8000/openapi.json` (50 KB).

Con esto la ruta A está terminada y el sistema está funcionando.

---

## 4. Ruta B — con la clave de Gemini

Solo cambia **quién redacta**. El motor de hechos, el verificador, la evidencia y la auditoría
son idénticos.

### 4.1 Conseguir la clave

La clave de la API de Gemini se obtiene en **Google AI Studio** (`aistudio.google.com`), en la
sección de claves de API. `[NO VERIFICADO]` — no se pudo comprobar desde este entorno, que no
tiene salida a internet. Consulte la documentación vigente de Google, porque la ubicación y el
nombre de esa sección cambian.

### 4.2 Dónde ponerla

Cree un archivo `.env` en la raíz del repositorio (está en `.gitignore`; **nunca** se sube).
Tome `.env.example` como plantilla:

```dotenv
LLM_MODE=gemini
GEMINI_API_KEY=su-clave-aqui
GEMINI_MODEL=            # el id vigente; ver el aviso de abajo
GEMINI_EMBED_MODEL=      # opcional, para embeddings reales
LLM_TIMEOUT_S=4
```

O por variables de entorno, sin tocar archivos:

```bash
# bash
LLM_MODE=gemini GEMINI_API_KEY=su-clave GEMINI_MODEL=<id> python -m uvicorn apps.api.main:app --port 8000
```

```powershell
# PowerShell
$env:LLM_MODE       = "gemini"
$env:GEMINI_API_KEY = "su-clave"
$env:GEMINI_MODEL   = "<id>"
python -m uvicorn apps.api.main:app --port 8000
```

Necesita además el SDK, que es dependencia declarada del proyecto (`google-genai>=1.0,<2.0`) y
entra con `pip install -e ".[dev]"`. Si lo instaló a mano y le falta, vea el
[problema 5](#5-el-paquete-google-genai-no-está-instalado).

> ### Aviso sobre el identificador del modelo
>
> **El id del modelo no está fijado en el código y debe verificarlo en la documentación vigente
> de Google antes de usarlo.** El proyecto lo lee de `GEMINI_MODEL`; solo si esa variable está
> vacía cae a un valor por defecto que el propio módulo marca como `[POR VALIDAR]`
> (`packages/llm_layer/providers/gemini.py`, constante `MODELO_POR_DEFECTO`). Las familias y los
> sufijos de versión cambian con frecuencia y un id caducado devuelve `404 NOT_FOUND`. Lo mismo
> vale para `GEMINI_EMBED_MODEL`: si no lo fija, los embeddings usan el `MockEmbedder` y el log
> lo dice con un `WARNING` explícito.

### 4.3 Qué cambia

| | `LLM_MODE=mock` | `LLM_MODE=gemini` |
|---|---|---|
| Quién redacta | Plantillas determinísticas del repositorio | El modelo de Google |
| Red | No hace ninguna llamada | Una llamada HTTPS por turno |
| Reproducibilidad | Byte a byte | Alta pero no garantizada (`temperature=0`, `candidate_count=1`) |
| Latencia típica | 7–37 ms (medido) | La de la red `[NO VERIFICADO]` |
| Cifras de la respuesta | Del `FactSet` | Del `FactSet` — **el verificador es el mismo y bloquea igual** |

### 4.4 Cómo comprobar que está usando Gemini de verdad y no el mock

Mire dos campos de cualquier respuesta de `POST /v1/explicar`:

| Campo | Con mock (verificado) | Con Gemini | Degradado a plantilla (verificado) |
|---|---|---|---|
| `gobernanza.modo` | `LLM` | `LLM` o `LLM_REINTENTO` | `PLANTILLA` |
| `gobernanza.model_version` | `mock-plantillas-1.0.0` | `gemini:<id-del-modelo>` | `plantilla-determinista` |
| `telemetria.proveedor` | `mock` | `gemini` | `plantilla` |

Y la sonda de preparación:

```bash
curl -s http://127.0.0.1:8000/salud/preparacion
```

```jsonc
// modo mock, verificado:
"llm": {"modo": "mock", "proveedor": "mock", "degradado": false}

// LLM_MODE=gemini sin credencial utilizable, verificado:
"llm": {"modo": "gemini", "proveedor": "plantilla-determinista", "degradado": true}
```

**`degradado: true` significa que puso `gemini` pero está respondiendo la plantilla.** Es el
error más fácil de cometer en una demo. La causa se ve en el log de arranque:

```
WARNING apps.api.deps | no hay proveedor LLM (CONFIGURACION_INVALIDA): se responderá con plantilla determinística
```

En la consola `/ui` esto se ve sin tocar nada: el indicador **Modo del modelo** de la columna
derecha lee `GET /salud` y muestra cuál está activo.

Extracción del campo sin `jq`:

```bash
curl -s -X POST http://127.0.0.1:8000/v1/explicar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"periodo":"2026-07","canal":"APP","utterance":"por que subio mi recibo"}' \
| python -c "import sys,json;d=json.load(sys.stdin);g=d['gobernanza'];print(g['modo'],g['model_version'],d['telemetria']['proveedor'])"
```

Importante: **una clave inválida nunca tumba el servicio.** El turno devuelve `200` y la respuesta
la escribe la plantilla. Nunca hay un `424` ni un `502` por culpa del modelo.

Sobre la cabecera `X-Degradado: PLANTILLA`, un matiz que conviene conocer porque se presta a
confusión (comprobado en las dos situaciones):

| Situación | `gobernanza.modo` | `X-Degradado` | Dónde se ve |
|---|---|---|---|
| El proveedor **existe** y la llamada falla en caliente (timeout, error del modelo) | `PLANTILLA` | **sí**, `PLANTILLA` | cabecera de la respuesta |
| El proveedor **no llega a construirse** (sin `GEMINI_API_KEY`, clave vacía o SDK ausente) | `PLANTILLA` | **no** | `/salud/preparacion` → `"degradado": true`, y el `WARNING` del arranque |

La cabecera marca *este turno degradó*; la sonda marca *este proceso arrancó sin modelo*. El
segundo caso es el que produce el ejemplo de arriba (`GEMINI_API_KEY=` vacía) y el que se ve en la
[prueba 7.b](#7b-apague-el-modelo-y-compruebe-que-las-cifras-no-cambian): ahí la respuesta llega
sin cabecera, y el sitio donde mirar es `gobernanza.model_version = plantilla-determinista`.

---

## 5. Ruta C — Docker completo con PostgreSQL y pgvector

Es la ruta que más se acerca a producción: PostgreSQL 16 con pgvector persistiendo el índice del
RAG, la API en una imagen multi-stage sin compilador, y **los dos sistemas de Movistar simulados
como servicios HTTP independientes** (`mock-brainybill:8801`, `mock-amdocs:8802`), para que el
Anti-Corruption Layer hable HTTP contra un tercero desde el primer día.

> `[NO VERIFICADO DE EXTREMO A EXTREMO]` — en el equipo donde se redactó este manual están
> instalados Docker 27.1.1 y Docker Compose v2.29.1, **pero el demonio no estaba corriendo**
> (`error during connect: ... open //./pipe/dockerDesktopLinuxEngine: The system cannot find the
> file specified`), así que no se pudo construir la imagen ni levantar PostgreSQL. Los comandos
> de abajo se transcriben de `Makefile`, `docker-compose.yml` y `Dockerfile.api`, que sí se
> leyeron. Lo que **sí** se verificó por separado está marcado como tal.

### 5.1 En un comando

```bash
make demo
```

Encadena, en este orden (y el orden importa: la API cachea el corpus del RAG al arrancar, así que
se siembra e indexa **antes** de levantarla):

| Paso | Objetivo | Qué hace |
|---|---|---|
| 1/5 | `build` | Construye `recibo-claro:local` desde `Dockerfile.api` (multi-stage: `constructor` instala en un venv, `runtime` solo lo copia) |
| 2/5 | `migrate` | Levanta `db` y aplica `db/migraciones/001_core.sql`, `002_rag.sql`, `003_auditoria.sql` |
| 3/5 | `seed` | Genera el dataset determinístico (semilla `20260804`, 300 clientes) |
| 4/5 | `indexar` | Vectoriza catálogo, FAQs y casuísticas en pgvector |
| 5/5 | `up` + `smoke` | Levanta `db`, `api`, `mock-brainybill`, `mock-amdocs` y ejecuta la prueba de humo |

Al terminar: `http://127.0.0.1:8000/ui`, `http://127.0.0.1:8000/docs`,
`http://127.0.0.1:8801/salud`, `http://127.0.0.1:8802/salud`.

### 5.2 Sin `make` (Windows)

`make` no viene con Windows. Los comandos crudos equivalentes:

```bash
docker compose build
docker compose up -d --wait db
docker compose run --rm --no-deps api python -m db.migrar
docker compose run --rm --no-deps api python -m packages.datagen.generar --seed 20260804 --clientes 300 --periodo-actual 2026-07 --salida data/sintetico
docker compose run --rm --no-deps api python -m packages.retriever.indexar --verbose
docker compose up -d --build --wait
docker compose exec -T api python /app/docker/smoke.py --api http://127.0.0.1:8000 --cuenta C-DEMO-01 --periodo 2026-07 --verbose
```

Para apagar: `docker compose down` (conserva la base) o `docker compose down -v` (la borra).

### 5.3 Qué cambia respecto de la ruta A

| | Ruta A | Ruta C |
|---|---|---|
| Índice vectorial | En memoria del proceso, se reconstruye en cada arranque | En pgvector, persiste entre reinicios |
| Origen de los datos | Disco (`TransporteArchivo`) | HTTP contra los dos mocks (`TransporteHTTP`) |
| `DATABASE_URL` | vacía | `postgresql://recibo:recibo@db:5432/recibo` (la inyecta `docker compose`) |
| Cifras de la respuesta | — | **idénticas** |

Esa última fila **sí se verificó**, y sin Docker: levantando los dos mocks a mano y apuntando la
API contra ellos por HTTP.

```bash
# terminal 1
BRAINYBILL_DATOS=data/sintetico python -m uvicorn apps.mocks.brainybill.servidor:app --port 8801
# terminal 2
AMDOCS_DATOS=data/sintetico/ordenes.csv python -m uvicorn apps.mocks.amdocs.servidor:app --port 8802
# terminal 3  — ojo con AUDIT_LOG_PATH: ver el aviso de abajo
BRAINYBILL_BASE_URL=http://127.0.0.1:8801 AMDOCS_BASE_URL=http://127.0.0.1:8802 \
  AUDIT_LOG_PATH=/tmp/rc-8020/eventos.jsonl TELEMETRIA_PATH=/tmp/rc-8020/sondas.jsonl \
  python -m uvicorn apps.api.main:app --port 8020
```

> **Si ya tiene otra instancia levantada, dele su propia bitácora.** Dos procesos escribiendo
> `data/auditoria/eventos.jsonl` a la vez **rompen la cadena de hashes**: los eventos se
> intercalan y dos consecutivos toman el mismo `hash_previo`. Reproducido de propósito en este
> equipo con dos instancias y ocho turnos cada una: `verificar_cadena() → (False, 21)`. A partir
> de ahí `/salud/preparacion` responde `503` con `"listo": false` y `probar_e2e.py` baja a
> `16/19`. Por eso las líneas de arriba fijan `AUDIT_LOG_PATH` y `TELEMETRIA_PATH` para la
> instancia secundaria. La otra salida válida es apagar la primera instancia antes. Detalle
> completo en el [problema 17](#17-cadena_valida-false--la-bitácora-se-rompió).
> En PowerShell: `$env:AUDIT_LOG_PATH = "$env:TEMP\rc-8020\eventos.jsonl"`.

**Qué debería ver** (salidas reales):

```json
// GET http://127.0.0.1:8801/salud
{"estado":"ok","servicio":"mock-brainybill","datos":"data\\sintetico\\bills","cuentas":300}

// GET http://127.0.0.1:8802/salud
{"estado":"ok","servicio":"mock-amdocs","datos":"data\\sintetico\\ordenes.csv","cuentas":266,
 "ordenes":432,"columnas":["ORDER_ID","ACCOUNT_ID","ORDER_TYPE","ORDER_DATE","SERVICE_ID","CHANNEL","DETAIL_JSON"]}

// GET http://127.0.0.1:8020/salud/sistemas   <-- la API ya no lee del disco
{"brainybill":{"destino":"http://127.0.0.1:8801","transporte":"TransporteHTTP","alcanzable":true},
 "amdocs":{"destino":"http://127.0.0.1:8802","transporte":"TransporteHTTP","alcanzable":true},
 "ciclos":6}
```

Y la explicación por HTTP, comparada con la de disco:

```
PASS · 12 aserciones · 0 no ancladas · modo LLM
factset_sha256 = 3227801e4fcca4c4b48f922010ac8e2679d403d8e704dbd278f7e1963383a4aa
"Su recibo de julio de 2026 llegó S/ 20.82 más alto que el de junio de 2026 porque cambió de
 plan a mitad de mes y su renta se cobra por adelantado."
```

**Idéntica al céntimo y con el mismo sello.** Cambiar el origen de los datos no cambia el
resultado: eso es lo que compra el Anti-Corruption Layer.

Otras rutas útiles de los mocks (verificadas):

```
GET http://127.0.0.1:8801/bills/C-DEMO-01?cycles=2
GET http://127.0.0.1:8802/orders/C-DEMO-01?formato=amdocs
GET http://127.0.0.1:8802/orders/C-DEMO-01/validacion
    -> {"cuenta_id":"C-DEMO-01","apto":true,"errores":[],"filas":2}
```

### 5.4 Comprobar el estado de PostgreSQL sin levantarlo

Estos dos corren sin base de datos:

```bash
python -m db.migrar --listar
```

```
Migraciones locales (3):
  001_core                 c4fe99baed98…    35053 bytes
  002_rag                  537424b4947e…    13416 bytes
  003_auditoria            b9a527b4b27d…    12384 bytes
```

```bash
python -m packages.retriever.indexar --memoria --consulta "por que subio mi recibo"
```

```
indexado del corpus RAG
  origen        : ...\data\sintetico
  modelo        : mock:768 (dim 768)
  respaldo      : memoria
  degradación   : modo memoria solicitado explícitamente
  bm25          : 36 FAQs (rank_bm25)
  corpus:
    concepto_catalogo    total   31 · vectorizados   31 · sin cambios    0 · en índice   31
    faq                  total   36 · vectorizados   36 · sin cambios    0 · en índice   36
    casuistica           total   28 · vectorizados   28 · sin cambios    0 · en índice   28
  total en índice: 95

  búsqueda de humo: 'por que subio mi recibo'
     0.4411  faq:FAQ_RECONEXION_Y_DEVOLUCION     Me devolvieron los días sin servicio pero igual mi recibo subió, ¿por qué?
     0.3347  faq:FAQ_IGV                         ¿Qué es el impuesto que aparece en mi recibo?
     0.3178  faq:FAQ_PLAN_MAS_BARATO_RECIBO_SUBE Cambié a un plan más barato, ¿por qué mi recibo subió?
     0.2980  faq:FAQ_DONDE_VEO_DETALLE           ¿Dónde puedo ver el detalle de mi recibo?
     0.2660  faq:FAQ_CICLO_FACTURACION           ¿Por qué mi recibo no va del primero al último día del mes?
```

Con la base levantada, quite `--memoria` y añada `--estricto` para que falle si no pudo
persistir. `[NO VERIFICADO]`

---

## 6. El recorrido de prueba: los tres clientes de guion

El dataset trae 300 clientes, pero tres están reservados para la demo y siempre tienen los
mismos números. Periodo actual **`2026-07`** en los tres, previo `2026-06`, seis periodos
disponibles (`2026-07` … `2026-02`), moneda PEN.

En la consola `/ui` se cambia de cliente con el selector de la esquina superior izquierda. Por
línea de comandos, prepare un token para cada uno. En bash:

```bash
API=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"C-DEMO-01","nivel":"LOA2","canal":"APP"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
```

En PowerShell (verificado):

```powershell
$API = "http://127.0.0.1:8000"
$TOKEN = (Invoke-RestMethod -Method Post -Uri "$API/dev/token" -ContentType 'application/json' `
          -Body '{"cuenta_id":"C-DEMO-01","nivel":"LOA2","canal":"APP"}').access_token
```

`POST /dev/token` solo existe con `ENTORNO=dev`, que es el valor por defecto. En cualquier otro
entorno el token lo emite el IdP de Movistar y este endpoint no se monta.

**La cuenta sale siempre del token, nunca del cuerpo de la petición.** Pedir los hechos de otra
cuenta devuelve `403 CUENTA_NO_AUTORIZADA` (verificado).

---

### 6.1 `C-DEMO-01` — cambió a un plan más barato y le vino más caro

**Quién es:** cliente PREMIUM, móvil, **renta ADELANTADA**, ciclo del 1 de julio al 1 de agosto
de 2026, vence el **13/08/2026**, plan vigente **Plan Movil Max 50GB** (`OF003` del catálogo
oficial de ofertas de Movistar).

**Qué le pasó ese mes:** cambió de plan a mitad de ciclo (orden `53336292`). Al cambiar, perdió
el descuento por permanencia que estaba atado al plan anterior. Como su renta se cobra por
adelantado, en el mismo documento se juntan el ajuste de los días ya usados con el plan viejo y
la renta completa del mes siguiente con el plan nuevo.

**Descomposición del recibo** (`GET /v1/hechos?periodo=2026-07`, todo en céntimos enteros):

| Concepto | Junio | Julio | Δ | Clase | Causa |
|---|---:|---:|---:|---|---|
| `DESCUENTO_PROMOCIONAL` (Descuento por permanencia) | −4 990 | 0 | **+4 990** | DESAPARECIDO | CAMBIO_PLAN |
| `RENTA_PLAN_MOVIL` (Plan móvil) | 9 990 | 7 990 | **−2 000** | BAJO | CAMBIO_PLAN |
| `AJUSTE_RETROACTIVO_RENTA` (Ajuste del mes anterior) | 0 | −1 226 | **−1 226** | NUEVO | CAMBIO_PLAN |
| `IGV` | 1 015 | 1 333 | **+318** | SUBIO | derivado |
| **Total del recibo** | **19 555** | **21 637** | **+2 082** | | |

En soles: S/ 195.55 → S/ 216.37, **S/ 20.82 más**. Invariante:
`{"ok": true, "residual_cent": 0, "suma_deltas_cent": 2082, "delta_total_cent": 2082}`.
Causas agregadas: *cambio de plan* 1 764 c (84.73 %) e *IGV* 318 c (15.27 %).
Sello: `sha256 = 3227801e4fcca4c4b48f922010ac8e2679d403d8e704dbd278f7e1963383a4aa`.

**Qué preguntar:**

```bash
curl -s -X POST $API/v1/explicar -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "conversation_id": "11111111-1111-4111-8111-111111111111",
  "periodo": "2026-07", "verbosidad": "CORTO", "canal": "APP",
  "utterance": "¿por qué me vino más caro este mes si cambié a un plan más barato?"}'
```

**Qué respuesta esperar** — 5 bloques, en este orden (texto real):

```
[texto]   Su recibo de julio de 2026 llegó S/ 20.82 más alto que el de junio de 2026 porque
          cambió de plan a mitad de mes y su renta se cobra por adelantado.

[kv]      Su recibo en números
          Recibo de junio de 2026   S/ 195.55   (19555)  factset:total_previo_cent
          Recibo de julio de 2026   S/ 216.37   (21637)  factset:total_actual_cent
          Diferencia                S/  20.82   ( 2082)  factset:delta_total_cent

[puente]  De un mes a otro
          Recibo de junio de 2026   19555  entrada       factset:total_previo_cent
          Cambio de plan             1764  incremento    causa:CAMBIO_PLAN.monto_cent
          Igv                         318  incremento    causa:SIN_CAUSA.monto_cent
          Recibo de julio de 2026   21637  total         factset:total_actual_cent

[texto]   Qué cambió
          El cambio de plan representa S/ 17.64: en este documento se juntan el ajuste de los
          días que ya usó con el plan anterior y la renta del mes que viene con el plan nuevo,
          que se carga adelantada. Igv: aporta S/ 3.18 en este recibo.

[texto]   Recuerde que su plan ya incluye llamadas ilimitadas a todo destino nacional y roaming
          incluido en la Comunidad Andina, sin costo adicional. Si desea, le muestro el detalle
          línea por línea o lo comunico con un asesor.
```

Acciones ofrecidas: `VER_ALTERNATIVAS`, `REGISTRAR_CONSULTA`, `DERIVAR_ASESOR`.

**Qué mirar en la terminal de gobernanza** (columna derecha de `/ui`, o
`GET /v1/auditoria?trace_id=<trace>&incluir_eventos=false`):

```
╭─ RECIBO CLARO · trace tr-a46dc92946ff ──────────────────╮
│ AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 12 · NO ANCLADAS 0 │
╰─────────────────────────────────────────────────────────╯
  ✔ PETICIÓN    C-DEMO-01 · 2026-07 · APP · LOA2
  ✔ HECHOS      Δ +S/ 20.82 · 4 líneas · residual 0 c · invariante OK
  ✔ CONTEXTO    5 faq · 1 casuística · saneado · U=0.43
  ✔ GENERACIÓN  mock · mock-plantillas-1.0.0 · 36 ms
  ✔ VERIFICA    PASS · 12 ancladas · 0 derivadas · 0 no ancladas · 12 citas
  ✔ RESPUESTA   5 bloques · 3 acciones · LLM · 37 ms · cadena íntegra (11 eventos)
```

Lo importante: **`residual 0 c`** (la aritmética cierra) y **`0 no ancladas`** (ninguna cifra se
inventó). `GET /v1/evidencia/<trace>` devuelve **24 items** de tipos `factset, linea, mov, cat,
tramo, faq, casuistica`, por ejemplo:

```json
{"tipo":"factset","ref_id":"89df0978-...","fact_id":"factset:delta_total_cent",
 "snippet":"Recibo 2026-06 S/ 195.55 → recibo 2026-07 S/ 216.37: S/ 20.82. Residual de conciliación: 0 céntimos."}
{"tipo":"mov","ref_id":"53336292","fact_id":"linea:DESCUENTO_PROMOCIONAL.movimiento_id",
 "snippet":"Orden 53336292 del historial de Amdocs, tipo CAMBIO_PLAN, atribuida a DESCUENTO_PROMOCIONAL."}
```

Es lo mismo que abre el panel *"De dónde salió esta cifra"* de la consola.

> **Defecto conocido en este caso — léalo antes de enseñarlo.** La aritmética es exacta, pero la
> narrativa causal no lo es del todo: el motor agrupa las tres primeras líneas bajo *cambio de
> plan*, cuando el cambio de plan por sí solo le **bajó** el recibo S/ 32.26; lo que lo subió fue
> la desaparición del descuento de S/ 49.90. Está diagnosticado, con plan de corrección y
> estimación, en [`docs/pendientes.md`](pendientes.md) §1 (riesgo R-07). No afecta a ninguna
> cifra ni al veredicto del verificador.

---

### 6.2 `C-DEMO-02` — le cortaron el servicio, se lo reconectaron, y el recibo subió

**Quién es:** cliente MASIVO, móvil, **renta VENCIDA**, ciclo del 5 de julio al 5 de agosto de
2026, vence el **17/08/2026**, plan **Plan Movil Plus 25GB** (`OF002` del catálogo oficial).

**Qué le pasó ese mes:** el servicio estuvo suspendido nueve días (orden `65431801`) y se
reconectó (orden `65431802`). Le cobran S/ 25.00 por reconectar, pero le descuentan S/ 17.39 por
los días sin servicio. El neto sube S/ 8.98. Es el caso que más segundas llamadas genera al 104.

**Descomposición del recibo:**

| Concepto | Junio | Julio | Δ | Clase | Causa |
|---|---:|---:|---:|---|---|
| `CARGO_RECONEXION` (Reconexión del servicio) | 0 | 2 500 | **+2 500** | NUEVO | RECONEXION |
| `RENTA_PLAN_MOVIL` (Plan móvil) | 5 990 | 4 251 | **−1 739** | BAJO | SUSPENSION |
| `IGV` | 1 238 | 1 375 | **+137** | SUBIO | derivado |
| **Total del recibo** | **8 118** | **9 016** | **+898** | | |

S/ 81.18 → S/ 90.16, **S/ 8.98 más**. Residual 0. Causas: *reconexiones* 2 500 c (57.13 %),
*ajustes por días de suspensión* −1 739 c (39.74 %), *IGV* 137 c (3.13 %).
Sello: `e979cd8313e85454fa8f123395eff612a5d45e4d55c8c59b47068eec876d179b`.

La renta se descompone en **tres tramos** que suman exactamente los 4 251 c:

| Tramo | Días | Tarifa mensual | Estado | Cobrado |
|---|---:|---:|---|---:|
| del 5 al 10 de julio | 6 | S/ 59.90 | ACTIVO | S/ 11.59 |
| del 11 al 19 de julio | 9 | S/ 59.90 | SUSPENDIDO | no se cobró |
| del 20 de julio al 4 de agosto | 16 | S/ 59.90 | ACTIVO | S/ 30.92 |

**Qué preguntar:**

```bash
curl -s -X POST $API/v1/explicar -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' -d '{
  "conversation_id": "22222222-2222-4222-8222-222222222222",
  "periodo": "2026-07", "verbosidad": "CORTO", "canal": "APP",
  "utterance": "me cortaron el servicio y aun así me cobran, ¿por qué subió mi recibo?"}'
```

**Qué respuesta esperar:**

```
[texto]   Su recibo de julio de 2026 le llegó S/ 8.98 más caro que el de junio de 2026 porque
          le cortaron el servicio y luego se lo reactivaron.

[kv]      Recibo de junio de 2026  S/ 81.18 · Recibo de julio de 2026  S/ 90.16 · Diferencia  S/ 8.98

[puente]  Recibo de junio de 2026        8118   entrada
          Reconexiones                   2500   incremento   causa:RECONEXION.monto_cent
          Ajustes por días de suspensión −1739  decremento   causa:SUSPENSION.monto_cent
          Igv                             137   incremento
          Recibo de julio de 2026        9016   total

[texto]   Qué cambió
          La reactivación del servicio explica S/ 25.00: es el cargo que se cobra una sola vez
          por volver a conectarle la línea después del corte. Por los días que estuvo sin
          servicio se le aplicó un ajuste de S/ 17.39: los días cortados no se le cobran.
          Igv: le aporta S/ 1.37 en el recibo de este mes.

[texto]   Recuerde que su plan ya incluye redes sociales que no consumen sus datos y acceso a
          Movistar TV App incluido, sin costo adicional. ...
```

Acciones: `VER_DETALLE`, `REGISTRAR_CONSULTA`, `DERIVAR_ASESOR`.

**Pida el detalle** — repita con `"verbosidad": "DETALLE"` (o pulse *Detalle* en la consola) y
aparecen 7 bloques, entre ellos la tabla de tramos (real):

```
[tabla]   Detalle por tramos del mes
          Periodo                          Días  Tarifa mensual  Cobrado
          del 5 al 10 de julio               6      S/ 59.90     S/ 11.59
          del 11 al 19 de julio              9      S/ 59.90     no se cobró
          del 20 de julio al 4 de agosto    16      S/ 59.90     S/ 30.92
          nota: El ciclo completo de facturación tiene 31 días.
```

En `DETALLE` el verificador audita **49 cifras** y las ancla las 49.

**Terminal de gobernanza (CORTO):**

```
╭─ RECIBO CLARO · trace tr-a59b73a884de ──────────────────╮
│ AFIRMACIONES NUMÉRICAS 14 · ANCLADAS 14 · NO ANCLADAS 0 │
╰─────────────────────────────────────────────────────────╯
  ✔ HECHOS      Δ +S/ 8.98 · 3 líneas · residual 0 c · invariante OK
  ✔ VERIFICA    PASS · 14 ancladas · 0 derivadas · 0 no ancladas · 14 citas
  ✔ RESPUESTA   5 bloques · 3 acciones · LLM · 7 ms · cadena íntegra (11 eventos)
```

Evidencia: 21 items. **Fíjese en `S/ 13.53` y `S/ 36.08`**: no están literalmente en el recibo,
son prorrateos; el verificador los acepta como **DERIVADAS** por álgebra registrada, no como
invenciones.

---

### 6.3 `C-DEMO-03` — se le acabó la promoción y además arrastra deuda

**Quién es:** cliente HOGAR, fibra, **renta VENCIDA**, ciclo del 10 de julio al 10 de agosto de
2026, vence el **22/08/2026**, plan **Internet Hogar 100Mb** (`OF005` del catálogo oficial) más
**TV Hogar Sola** (`OF007`).

**Qué le pasó ese mes:** terminó el descuento de bienvenida (orden `80170101`), le cargaron un
interés por pago fuera de fecha y, sobre todo, **no pagó el recibo anterior**: arrastra
S/ 153.16.

**Descomposición del recibo:**

| Concepto | Junio | Julio | Δ | Clase | Causa |
|---|---:|---:|---:|---|---|
| `DESCUENTO_PROMOCIONAL` (Descuento de bienvenida) | −3 000 | −1 355 | **+1 645** | SUBIO | FIN_DESCUENTO |
| `IGV` | 2 336 | 2 632 | **+296** | SUBIO | derivado |
| `INTERES_MORATORIO` (Interés por pago fuera de fecha) | 0 | 230 | **+230** | NUEVO | CARGOS_ADICIONALES |
| **Total del recibo** | **15 316** | **17 487** | **+2 171** | | |
| Deuda de recibos anteriores | | **15 316** | | | |
| **TOTAL A PAGAR** | | **32 803** | | | |

S/ 153.16 → S/ 174.87 (**S/ 21.71 más**), pero **lo que debe pagar es S/ 328.03**. Residual 0.
Causas: *promociones vencidas* 1 645 c (75.77 %), *IGV* 296 c (13.64 %), *cargos adicionales*
230 c (10.59 %).
Sello: `0eafac9594e73c52de17e14dc9d4b4dcd60fb01d8912dd855896716a40a28f79`.

**Qué preguntar:**

```bash
curl -s -X POST $API/v1/explicar -H "Authorization: Bearer $TOKEN3" -H 'Content-Type: application/json' -d '{
  "conversation_id": "33333333-3333-4333-8333-333333333333",
  "periodo": "2026-07", "verbosidad": "CORTO", "canal": "APP",
  "utterance": "¿por qué me llegó más caro y qué es ese saldo pendiente?"}'
```

**Qué respuesta esperar** — 6 bloques; el quinto es un **aviso** que separa el recibo del mes del
total a pagar, que es la confusión más cara de este escenario:

```
[texto]   Su recibo de julio de 2026 llegó S/ 21.71 más caro que el de junio de 2026 debido a que
          terminó el descuento promocional que tenía y su plan volvió a su precio regular.

[kv]      Su recibo en números
          Recibo de junio de 2026      S/ 153.16  (15316)
          Recibo de julio de 2026      S/ 174.87  (17487)
          Diferencia                   S/  21.71  ( 2171)
          Saldo de recibos anteriores  S/ 153.16  (15316)  factset:deuda_anterior_cent
          Total a pagar                S/ 328.03  (32803)  factset:total_a_pagar_cent

[puente]  15316 entrada · Promociones vencidas +1645 · Igv +296 · Cargos adicionales +230 · 17487 total

[texto]   Qué cambió
          El fin de la promoción explica S/ 16.45: el descuento que se aplicaba a su plan llegó a
          su última mensualidad y ya no figura en este documento. Igv: aporta S/ 2.96 en este
          recibo. Cargos adicionales: suma S/ 2.30 en este documento.

[aviso · advertencia]
          Tiene un saldo pendiente de S/ 153.16 de recibos anteriores. Sumado a este recibo, el
          total a pagar es S/ 328.03.

[texto]   Además, se arrastra un saldo pendiente de S/ 153.16 ... el total a pagar queda en
          S/ 328.03. ...
```

Acciones: **`PAGAR`**, `REGISTRAR_CONSULTA`, `DERIVAR_ASESOR`. Que la primera acción sea pagar
—y no una oferta comercial— es deliberado.

**Terminal de gobernanza:**

```
╭─ RECIBO CLARO · trace tr-eac3e502cba9 ──────────────────╮
│ AFIRMACIONES NUMÉRICAS 20 · ANCLADAS 20 · NO ANCLADAS 0 │
╰─────────────────────────────────────────────────────────╯
  ✔ HECHOS      Δ +S/ 21.71 · 3 líneas · residual 0 c · invariante OK
  ✔ VERIFICA    PASS · 20 ancladas · 0 derivadas · 0 no ancladas · 20 citas
  ✔ RESPUESTA   6 bloques · 3 acciones · LLM · 10 ms · cadena íntegra (11 eventos)
```

Evidencia: 18 items.

### 6.4 Resumen de los tres

| | `C-DEMO-01` | `C-DEMO-02` | `C-DEMO-03` |
|---|---|---|---|
| Escenario | Cambio de plan a mitad de ciclo | Corte y reconexión | Fin de descuento + deuda |
| Renta | **ADELANTADA** | VENCIDA | VENCIDA |
| Recibo previo | S/ 195.55 | S/ 92.98 | S/ 153.16 |
| Recibo actual | S/ 216.37 | S/ 98.54 | S/ 174.87 |
| Diferencia | **+S/ 20.82** | **+S/ 5.56** | **+S/ 21.71** |
| Deuda anterior | — | — | **S/ 153.16** (a pagar S/ 328.03) |
| Vence | 13/08/2026 | 17/08/2026 | 22/08/2026 |
| Cifras auditadas (CORTO) | 12 | 14 | 20 |
| No ancladas | **0** | **0** | **0** |
| Residual | **0 c** | **0 c** | **0 c** |
| Items de evidencia | 24 | 21 | 18 |

---

## 7. Las tres pruebas que hay que hacer sí o sí

Si solo tiene cinco minutos, haga estas tres. Son las que demuestran la tesis del proyecto.

### 7.a Inyecte una alucinación y vea el bloqueo

El sistema trae un modo adversario que **corrompe a propósito el texto ya generado** metiendo una
cifra que no está en el `FactSet`, para que usted vea al verificador cazarla. Existe solo con
`ENTORNO=dev` y exige nivel LOA2. En la consola es el botón rojo **Inyectar alucinación**; por
línea de comandos:

```bash
curl -s -X POST $API/dev/alucinar -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"activar": true, "delta_cent": 731, "turnos": 1, "cuenta_id": "C-DEMO-01", "periodo": "2026-07"}'
```

**Qué debería ver** (salida real): la respuesta trae una demostración ya ejecutada que compara el
mismo turno limpio y envenenado.

```json
{"activo": true, "delta_cent": 731, "turnos_restantes": 1,
 "aviso": "Modo adversario activo: el próximo POST /v1/explicar recibirá una cifra inventada en el texto ya generado. El verificador debe cazarla, la respuesta debe bloquearse y el turno debe terminar en derivación con NO ANCLADAS > 0 en el log.",
 "demo": {
   "cuenta_id": "C-DEMO-01", "periodo": "2026-07",
   "factset_sha256": "3227801e4fcca4c4b48f922010ac8e2679d403d8e704dbd278f7e1963383a4aa",
   "veredicto_limpio": "PASS",      "no_ancladas_limpio": 0,
   "veredicto_envenenado": "FAIL",  "no_ancladas_envenenado": 1,
   "infractores": ["S/ 28.13"],     "tokens_infractores": ["cent:2813"],
   "terminal": [
     "VERIFICACION FAIL  factset=3227801e4fcc",
     "AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 11 · DERIVADAS 0 · NO ANCLADAS 1",
     "  NO ANCLADAS: S/ 28.13"],
   "conclusion": "la cifra inventada no está en el FactSet, el verificador la marca como NO_ANCLADA y la respuesta no llega al cliente"}}
```

El texto envenenado dice `"Su recibo de julio de 2026 llegó S/ 28.13 más alto..."` — 20.82 + 7.31.
Es una cifra plausible, redondeada al céntimo y en formato peruano correcto. **Ninguna heurística
de estilo la detectaría; el verificador la detecta porque `cent:2813` no está en el conjunto
permitido construido desde el `FactSet`.**

Ahora haga el turno de verdad, con el modo activo:

```bash
curl -s -X POST $API/v1/explicar -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"periodo":"2026-07","canal":"APP","utterance":"por qué subió mi recibo"}'
```

**Qué debería ver — esto es lo importante de toda la demo.** La respuesta llega con `HTTP 200`,
pero **no contiene ni una sola cifra**. Un solo bloque:

```
[aviso · advertencia]
   Prefiero no darle un número que no pueda sustentar con su recibo. Dejo su consulta con un
   asesor para que le confirme el detalle exacto.
```

```json
"gobernanza": {"anclado": false, "verificacion_numerica": "FAIL",
               "aserciones_totales": 12, "aserciones_ancladas": 11, "aserciones_no_ancladas": 1,
               "modo": "PLANTILLA"},
"derivacion": {"requerida": true, "motivo_codigo": "VERIFICACION_FALLIDA",
               "motivo": "no se pudo anclar toda la explicación numérica en el recibo",
               "context_ref": "ctx-5f9b18dba9065897", "senal_disparadora": "no_ancladas=1"},
"telemetria": {"adversaria": {"activo": true, "veredicto": "FAIL", "infractores": ["S/ 28.13"], "no_ancladas": 1}}
```

Acciones: solo `DERIVAR_ASESOR` y `REGISTRAR_CONSULTA`.

Y en la bitácora del mismo turno (`GET /v1/auditoria?trace_id=...`):

```
╭─ RECIBO CLARO · trace tr-5c37d86ac0d3 ──────────────────╮
│ AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 11 · NO ANCLADAS 1 │
╰─────────────────────────────────────────────────────────╯
  ✔ PETICIÓN    C-DEMO-01 · 2026-07 · APP · LOA2
  ✔ HECHOS      Δ +S/ 20.82 · 4 líneas · residual 0 c · invariante OK
  ▲ CONTEXTO    5 faq · 1 casuística · saneado · derivación: VERIFICACION_FALLIDA
  ✔ GENERACIÓN  mock · mock-plantillas-1.0.0 · 1 ms
  ✖ VERIFICA    FAIL · 11 ancladas · 0 derivadas · 1 no anclada · 11 citas
  ▲ RESPUESTA   1 bloque · 2 acciones · PLANTILLA · derivada a asesor · 0 ms · cadena íntegra
```

**Lea la línea `✖ VERIFICA`: ahí está la prueba.** El sistema prefirió callarse a decir un número
que no podía sustentar. El brief para el asesor se genera solo y ya lleva la variación real, la
causa y qué queda pendiente.

Apáguelo antes de seguir:

```bash
curl -s -X POST $API/dev/alucinar -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"activar": false}'
```

```json
{"activo": false, "delta_cent": 731, "turnos_restantes": 0, "aviso": "modo adversario desactivado", "demo": null}
```

---

### 7.b Apague el modelo y compruebe que las cifras no cambian

Levante una segunda instancia **sin ningún proveedor generativo disponible**. La forma limpia de
conseguirlo: pedir `gemini` sin credencial. El sistema no arranca a medias ni falla: se queda sin
modelo y responde con la plantilla determinística.

```bash
LLM_MODE=gemini GEMINI_API_KEY= \
  AUDIT_LOG_PATH=/tmp/rc-8010/eventos.jsonl TELEMETRIA_PATH=/tmp/rc-8010/sondas.jsonl \
  python -m uvicorn apps.api.main:app --port 8010
```

```powershell
$env:LLM_MODE = "gemini"; $env:GEMINI_API_KEY = ""
$env:AUDIT_LOG_PATH = "$env:TEMP\rc-8010\eventos.jsonl"
$env:TELEMETRIA_PATH = "$env:TEMP\rc-8010\sondas.jsonl"
python -m uvicorn apps.api.main:app --port 8010
```

> `AUDIT_LOG_PATH` y `TELEMETRIA_PATH` no son decorativos aquí: esta instancia convive con la del
> puerto 8000 y **dos procesos escribiendo la misma bitácora rompen la cadena de hashes**
> (reproducido: `verificar_cadena() → (False, 21)`). Ver el
> [problema 17](#17-cadena_valida-false--la-bitácora-se-rompió).

**Qué debería ver en el log:**

```
WARNING apps.api.deps | no hay proveedor LLM (CONFIGURACION_INVALIDA): se responderá con plantilla determinística
```

```json
// GET :8010/salud/preparacion
"llm": {"modo": "gemini", "proveedor": "plantilla-determinista", "degradado": true}
```

Ahora pida la misma explicación de `C-DEMO-01` contra el puerto 8010 y compárela con la del
puerto 8000. **Resultado real, lado a lado:**

| | Puerto 8000 (`modo: LLM`) | Puerto 8010 (`modo: PLANTILLA`) |
|---|---|---|
| `model_version` | `mock-plantillas-1.0.0` | `plantilla-determinista` |
| Diferencia del mes | **S/ 20.82** | **S/ 20.82** |
| Recibo previo / actual | 19 555 / 21 637 | 19 555 / 21 637 |
| Barra "Cambio de plan" | 1 764 | 1 764 |
| Barra "Igv" | 318 | 318 |
| Cambio de plan en el texto | **S/ 17.64** | **S/ 17.64** |
| IGV en el texto | **S/ 3.18** | **S/ 3.18** |
| `factset_sha256` | `3227801e4fcc…` | `3227801e4fcc…` |
| Verificación | PASS · 12/12 · 0 no ancladas | PASS · 12/12 · 0 no ancladas |

Lo único que cambia es el verbo:

```
LLM:       "El cambio de plan representa S/ 17.64: ... que se carga adelantada. Igv: aporta S/ 3.18 ..."
PLANTILLA: "El cambio de plan explica    S/ 17.64: ... que se cobra adelantada. Igv: suma   S/ 3.18 ..."
```

**Por qué esto demuestra que el modelo no calcula.** Si el modelo participara en la aritmética,
quitarlo cambiaría algún número, o el sello del `FactSet`, o el veredicto. No cambia ninguno de
los tres. El `FactSet` se construye **antes** de que exista cualquier texto —el sello
`3227801e4fcc…` es el mismo con modelo, sin modelo y leyendo los datos por HTTP en vez de por
disco— y el modelo solo elige palabras alrededor de cifras que ya estaban decididas. Es lo mismo
que dice `docs/ADR/003-el-llm-no-calcula.md`, pero comprobado en su terminal en 30 segundos.

---

### 7.c Provoque la derivación y vea que el sistema se niega

Hay tres formas de provocarla. Las tres están verificadas.

**Forma 1 — el cliente pide un humano** (regla dura: gana sobre cualquier score). En la consola,
la pastilla *"Quiero hablar con un asesor"*. Por línea de comandos:

```bash
curl -s -X POST $API/v1/explicar -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "conversation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "periodo": "2026-07", "canal": "APP",
  "utterance": "no me convence su explicación, quiero hablar con un asesor por favor"}'
```

Resultado real: la explicación **sí se entrega** (5 bloques, `PASS`, 12/12) **y además** se marca
la derivación con el contexto completo:

```json
"derivacion": {
  "requerida": true, "motivo_codigo": "PETICION_HUMANO",
  "senal_disparadora": "el cliente pidió atención humana (\"asesor\")",
  "score_incomprension": 0.4292, "context_ref": "ctx-596b006d35cc54e6",
  "resumen_asesor": "..." }
```

Ese `resumen_asesor` son siete líneas de brief, generadas solas:

```
CLIENTE      C-DEMO-01 · recibo 2026-07 · renta ADELANTADA · vence 13/08/2026
CONSULTA     «no me convence su explicación, quiero hablar con un asesor por favor» · canal APP
VARIACIÓN    S/ 195.55 → S/ 216.37 (S/ 20.82)
CAUSA        cambio de plan · S/ 17.64 (84.73%) · confianza 0.98 · orden 53336292
YA EXPLICADO explicación entregada · modo LLM · verificación PASS
DERIVA POR   el cliente pidió atención humana ("asesor")
PENDIENTE    atender la duda concreta del cliente; la explicación del recibo ya se le dio
```

**Forma 2 — intención regulatoria** (`utterance`: *"esto es un cobro indebido, voy a presentar un
reclamo formal ante Osiptel"*) → `motivo_codigo: "INTENCION_REGULATORIA"`,
`senal_disparadora: "intención regulatoria detectada (\"reclamo formal\")"`. También entrega la
explicación y además deriva. Un asistente de facturación no gestiona reclamos formales.

**Forma 3 — la que hace que el sistema se niegue a explicar:** cuando el verificador no puede
anclar una cifra. Es la de la [prueba 7.a](#7a-inyecte-una-alucinación-y-vea-el-bloqueo). Ahí, y
solo ahí, la respuesta se queda en un aviso sin números.

Conviene tener clara la diferencia, porque es una decisión de producto, no un descuido:

| Situación | ¿Explica? | ¿Deriva? |
|---|---|---|
| Turno normal | Sí | No |
| El cliente pide un asesor | **Sí** — no se le castiga quitándole la información | Sí |
| Intención regulatoria (Osiptel, reclamo, baja, portabilidad) | **Sí** | Sí |
| **El verificador marca `FAIL`** | **NO. Ni una cifra** | Sí, con `VERIFICACION_FALLIDA` |

Para pedir la derivación explícitamente, sin pasar por una explicación:

```bash
curl -s -X POST $API/v1/derivacion -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{
  "conversation_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "periodo": "2026-07",
  "motivo_codigo": "PETICION_HUMANO", "utterance": "quiero hablar con una persona"}'
```

**Qué debería ver** (real):

```json
{"trace_id": "tr-d47c1ecfe5ea", "context_ref": "ctx-db2c98e265715116",
 "cola": "FACTURACION_104", "prioridad": "NORMAL", "vigencia_min": 120, "lineas_brief": 7,
 "factset_sha256": "3227801e4fcca4c4b48f922010ac8e2679d403d8e704dbd278f7e1963383a4aa",
 "resumen_asesor": "CLIENTE      C-DEMO-01 · recibo 2026-07 · renta ADELANTADA · vence 13/08/2026\n..."}
```

Y el asesor recupera el contexto completo —incluido el `FactSet` entero— con
`GET /v1/derivacion/ctx-db2c98e265715116` (verificado, 200). El cliente no repite nada.

---

## 8. Cómo leer la terminal de gobernanza

El banner sale por cuatro sitios: en la columna derecha de la consola `/ui`, en el log del
servidor (`LOG_TERMINAL=true`, por defecto), en `GET /v1/auditoria?trace_id=...` (campo
`terminal`) y al final de `scripts/probar_e2e.py` y `docker/smoke.py`.

```
╭─ RECIBO CLARO · trace tr-a46dc92946ff ──────────────────╮
│ AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 12 · NO ANCLADAS 0 │      <-- el titular
╰─────────────────────────────────────────────────────────╯
  ✔ PETICIÓN    C-DEMO-01 · 2026-07 · APP · LOA2
  ✔ HECHOS      Δ +S/ 20.82 · 4 líneas · residual 0 c · invariante OK
  ✔ CONTEXTO    5 faq · 1 casuística · saneado · U=0.43
  ✔ GENERACIÓN  mock · mock-plantillas-1.0.0 · 36 ms
  ✔ VERIFICA    PASS · 12 ancladas · 0 derivadas · 0 no ancladas · 12 citas
  ✔ RESPUESTA   5 bloques · 3 acciones · LLM · 37 ms · cadena íntegra (11 eventos)
```

Símbolos: `✔` la etapa fue limpia · `▲` hubo una degradación o una derivación · `✖` la etapa
falló (y con `VERIFICA` en `✖` la respuesta no se entrega).

### Qué significa cada línea

| Línea | Etapas internas | Qué le dice |
|---|---|---|
| **PETICIÓN** | `REQUEST` | Quién pregunta, por qué periodo, por qué canal y con qué nivel de aseguramiento. La cuenta sale del token |
| **HECHOS** | `FACTS_BUILT`, `INVARIANTE` | La variación del mes, cuántas líneas cambiaron y —lo importante— el **residual** de la conciliación. `residual 0 c` significa que la suma de los deltas por línea es exactamente igual al delta del total. Si no cuadra, no se explica: se deriva |
| **CONTEXTO** | `RETRIEVE`, `ROUTE` | Cuántas FAQs y casuísticas se recuperaron, si el corpus se saneó (se le retiran las cifras al texto recuperado para que no contamine) y `U`, el score de incomprensión del cliente |
| **GENERACIÓN** | `LLM_CALL` | Quién redactó, con qué versión y en cuánto tiempo |
| **VERIFICA** | `VERIFY`, `CITATIONS` | El veredicto y el desglose de todas las cifras del texto. **Es la línea que hay que mirar** |
| **RESPUESTA** | `RESPONSE`, `CHAIN` | Qué se entregó y si la cadena de hashes de la bitácora sigue íntegra |

La bitácora es un JSONL append-only en `data/auditoria/eventos.jsonl` donde cada evento encadena
el SHA-256 del anterior: un turno normal deja **11 eventos** (`REQUEST`, `ROUTE`, `FACTS_BUILT`,
`INVARIANTE`, `RETRIEVE`, `ROUTE`, `LLM_CALL`, `VERIFY`, `CITATIONS`, `RESPONSE`, `CHAIN`) y un
turno derivado deja **12**. Los `ROUTE` son tres decisiones distintas y por eso se repite la
etapa: el primero clasifica la **intención** del mensaje (antes de tocar la facturación), el
segundo registra el **score de incomprensión**, y el tercero —solo si se deriva— anota el
**motivo de la derivación**. Alterar un evento a posteriori rompe la cadena y `cadena_valida`
pasa a `false`.

### ANCLADA, DERIVADA y NO ANCLADA

El verificador extrae **todas** las cifras del texto final (importes en las tres escrituras
peruanas, porcentajes, fechas en cuatro formatos, días, "cuota N de M") y las normaliza a tokens
del tipo `cent:2082`, `num:31`, `pct:8473`, `fecha:2026-08-13`. Después clasifica cada una:

| Estado | Qué significa | Ejemplo real de `C-DEMO-01` / `C-DEMO-02` |
|---|---|---|
| **ANCLADA** | El token está **literal** en el `FactSet` | `S/ 20.82` → `cent:2082` = `factset:delta_total_cent` |
| **DERIVADA** | No está literal, pero sale del `FactSet` por **álgebra permitida y registrada**: suma, resta, diferencia de fechas en días, cociente `días/D`, porcentaje y redondeo al céntimo. Queda anotada con su regla y sus operandos | `S/ 13.53` = 6 días × S/ 69.90 / 31 días (el prorrateo del primer tramo) |
| **NO ANCLADA** | No sale de ningún sitio. **Veredicto `FAIL`** | `S/ 28.13`, la cifra que inyecta el modo adversario |

Con `FAIL` la respuesta se reintenta una vez y, si vuelve a fallar, **no se entrega**: la
sustituye un aviso sin cifras y el turno se deriva. `VERIFICADOR_ESTRICTO=true` (por defecto) es
lo que hace que esa política se aplique.

### Por qué el contador en cero es la afirmación central del proyecto

La ficha del Desafío 1 no pide "pocas alucinaciones". Pide **"Tasa de Alucinación: cero
invenciones financieras comprobables mediante logs de la terminal"**. Eso es exactamente lo que
mide `NO ANCLADAS`.

Tres razones por las que ese contador vale más que un porcentaje de exactitud:

1. **Es una garantía estructural, no un promedio.** No se compara contra un juego de respuestas
   correctas: se compara contra el `FactSet` **del propio cliente**, sellado y trazable. Una
   métrica de exactitud puede dar 99 % y aun así el 1 % restante es un cobro inventado en la cara
   de un cliente.
2. **Se traslada tal cual a producción.** Es la única cifra de la evaluación que no depende del
   dataset sintético: el mecanismo es idéntico con datos reales de Movistar.
3. **Es comprobable por un tercero.** El log encadenado deja las 12 aserciones, su token, su
   estado y su fuente. Cualquiera puede rehacer la comprobación sin confiar en nosotros.

Un `NO ANCLADAS 0` en cada turno es, literalmente, el producto.

---

## 9. Los comandos de verificación

Los cinco que importan. Todos tienen atajo en el `Makefile`; como en Windows `make` no suele
estar, se da también el comando crudo.

### 9.1 La prueba de extremo a extremo — `make probar`

```bash
python scripts/probar_e2e.py --api http://127.0.0.1:8000
```

Exige una API levantada. Recorre 19 pasos —token, hechos, explicar en las dos verbosidades,
evidencia, derivación, auditoría, cadena de hashes, modo adversario, niveles y errores— y falla
si alguno no cumple lo que promete el producto. Salida completa y códigos de salida: ver el
[paso 4 de la ruta A](#paso-4--la-prueba-de-extremo-a-extremo).

Resumen de la última ejecución real: **`TODO PASA · 19/19 pasos en 1.98 s`**, código de salida
`0`.

Variantes: `--cuenta C-DEMO-02`, `--json`, `--esperar 60`.

### 9.2 La batería de pruebas — `make test`

```bash
python -m pytest
```

**Qué debería ver** (real, con `-p no:warnings` para que el resumen no se pierda entre avisos):

```
........................................................................ [ 16%]
...........sssssssssssssssssssssssssssssssssssssssssssssssssssssssssss.. [ 33%]
.sss.................................................................... [ 49%]
........................................................................ [ 66%]
........................................................................ [ 82%]
........................................................................ [ 99%]
..                                                                       [100%]
372 passed, 62 skipped in 10.06s
```

Código de salida `0`. **434 pruebas recogidas: 372 pasan y 62 se saltan a propósito.** Las 62 no
son un fallo:

```
SKIPPED [34] tests\golden\test_sin_numeros_no_anclados.py:69: sin GEMINI_API_KEY: la build no puede depender de una API externa
SKIPPED [28] tests\golden\test_sin_numeros_no_anclados.py:109: el caso no declara fragmentos prohibidos
```

Las 34 primeras vuelven a correr los casos golden contra el modelo real; se omiten porque una
build que depende de una API externa no es una build. Las 28 restantes son casos que no declaran
fragmentos prohibidos, así que esa comprobación concreta no aplica.

Marcadores disponibles: `golden`, `propiedad`, `contrato`, `lento`, `gemini`.
Ejemplo: `python -m pytest -m golden`.

### 9.3 La evaluación oficial — `make eval`

```bash
python -m eval.run_eval --detalle
```

También `--markdown` (para pegar en un documento) y `--json` (para CI).

**Qué debería ver** (extractos reales; son 261 casos golden y tarda ~4,5 s):

```
  [  1/261] ok ADV01_monto_falso                   14 cifras · 0 sin anclar · 103 ms
  [  2/261] ok ADV02_cuenta_ajena                  29 cifras · 0 sin anclar · 17 ms
  ...
  [261/261] ok HDF10_reclamo_formal                21 cifras · 0 sin anclar · 12 ms
╔════════════════════════════════════════════════════════════════════════════╗
║  RECIBO CLARO · EVALUACIÓN DE LAS MÉTRICAS OFICIALES                       ║
║  Desafío 1 · Hackathon AI Telecom 2026 · protocolo v1.0.0                  ║
╚════════════════════════════════════════════════════════════════════════════╝
  casos golden: 261   ·   proveedor: mock   ·   reglas: 1.0.0   ·   4443 ms

┌ 1. PRECISIÓN DE RECUPERACIÓN ──────────────────────────────────────────────┐
  (C) strict answer accuracy  ← TITULAR          100.00 %   261/261 respuestas exactas
  (A) field-level exact match · micro            100.00 %   1388/1388 campos
  (B) Recall@1 doc-level (concepto_id)           100.00 %   213 casos evaluables

┌ 2. TASA DE ALUCINACIÓN ────────────────────────────────────────────────────┐
  TA_respuesta  ← COMPROMETIDA EN 0                0.00 %   CUMPLE · 0/261 respuestas
  TA_asercion                                      0.00 %   0/4625 cifras
  afirmaciones numéricas auditadas                   4625   todas ancladas o derivadas del FactSet
  fragmentos prohibidos en el texto                     0   casos adversariales de inyección
  veredictos del verificador: PASS 261

┌ 3. PRECISIÓN DEL HAND-OFF ─────────────────────────────────────────────────┐
  Recall_handoff  ← PRIMARIA                     100.00 %   13 de 13 derivaciones debidas
  Precision_handoff                              100.00 %
  Tasa de atrapamiento (FP / FP+VN)                0.00 %
  Handoff_completeness (7 campos)                100.00 %   91/91 campos informados
  matriz  VP 13 · FP 0 · VN 248 · FN 0   (exactitud 100.00 %)

┌ MÉTRICAS DE APOYO ─────────────────────────────────────────────────────────┐
  residual_medio_cent                                0.00   máximo 0 c · tolerancia ±1 c
  precision_causa_raiz                           100.00 %   391/391 conceptos atribuidos
  tasa_fallback (a plantilla)                      0.00 %   LLM 261
  latencia por caso (mediana / p95)            13 / 29 ms   pipeline completo, sin red

  EVALUACIÓN APROBADA
```

Código de salida `0`. **La cifra que hay que mirar es `TA_respuesta = 0.00 %` sobre 4 625
afirmaciones numéricas auditadas en 261 casos.**

La propia salida abre con una advertencia de circularidad que conviene leer entera: el ground
truth y el sistema comparten autor, así que estas cifras validan **la mecánica del motor**, no el
desempeño sobre datos reales de Movistar. La única que se traslada tal cual es
`TA_respuesta = 0`, porque el verificador no compara contra el ground truth sino contra el
`FactSet` del propio cliente.

#### De dónde salen los 261 casos

Treinta y ocho están escritos a mano, uno a uno, en `eval/golden/01..08_*.yaml`: el guion de la
demo, los siete escenarios en cada modalidad, los compuestos, los controles, el hand-off, los
adversariales originales y los cuatro de atribución causal. Cada uno documenta una decisión.

Los otros 223 los produce un generador por **muestreo estratificado y reproducible por semilla**:

```bash
python -m eval.generar_golden              # reescribe eval/golden/09..12_*.yaml
python -m eval.generar_golden --resumen    # solo el desglose por estrato, sin escribir
python -m eval.generar_golden --comprobar  # ¿lo que hay en disco es lo que sale de la semilla?
```

El muestreo cruza los 8 escenarios × 2 modalidades × 2 verbosidades × 4 canales, mantiene la
proporción de casos compuestos del dataset (~30 %), y garantiza cuota mínima en celdas que el
azar podría dejar sin medir: delta positivo, negativo y cero; con deuda arrastrada y sin ella;
la cuota de equipo financiado en su primera, intermedia, avanzada y última mensualidad; los
cuatro segmentos comerciales; y la combinación *renta convergente + corte de servicio*, que es
la que destapó un defecto real de atribución al ampliar la suite.

**Las cifras esperadas no salen del motor**: `total_esperado_cent` y `delta_esperado_cent` se
leen de los documentos de `data/sintetico/bills`, y los conceptos y las causas, de
`ground_truth.csv`. Si el motor discrepa, el generador lo dice y sale con código 1 — pero
escribe los casos igual, porque bajar la expectativa para que «pase» convertiría la suite en un
espejo del sistema.

**Si resiembra el dataset, regenere los casos.** `python -m eval.generar_golden` y listo; el test
`tests/golden/test_suite_golden.py::test_los_ficheros_generados_se_reproducen_desde_la_semilla`
avisa si se olvida.

#### La otra convención de prorrateo

`convencion_prorrateo` es política global de `rules.yaml`, no un atributo del recibo, así que no
es un campo del caso golden. Se cubre ejecutando la misma suite con la variable de entorno:

```bash
CONVENCION_PRORRATEO=30_360 python -m eval.run_eval --modo mock
```

El resultado es idéntico al céntimo —los importes salen del recibo emitido, no de un recálculo—
y lo que cambia es la **confianza declarada**: con 30/360 sobre un ciclo de 31 días el motor
recalcula el prorrateo, ve que no reproduce el importe facturado y topa la confianza de esa línea
en 0,50. Es el comportamiento correcto, y `tests/golden/test_convenciones_prorrateo.py` fija que
cambiar de convención nunca puede **subir** la confianza.

### 9.4 La auditoría — `make audit`

Sin `make`, el mismo guion que ejecuta el `Makefile`:

```bash
python -c "from packages.governance.auditoria import formatear_para_terminal, registro_por_defecto; r=registro_por_defecto(); t=r.trazas(); print('bitacora :', r.ruta); print('turnos   :', len(t)); print(); print(formatear_para_terminal(r.leer(t[-1]), t[-1], color=False) if t else 'sin turnos'); print(); print('cadena_valida:', *r.verificar_cadena())"
```

**Qué debería ver** (real; el número de turnos crece con cada petición que haya hecho):

```
bitacora : ...\recibo-claro\data\auditoria\eventos.jsonl
turnos   : 178

╭─ RECIBO CLARO · trace tr-30f6123a6cf2 ──────────────────╮
│ AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 12 · NO ANCLADAS 0 │
╰─────────────────────────────────────────────────────────╯
  ✔ PETICIÓN    C-DEMO-01 · 2026-07 · APP · LOA2
  ✔ HECHOS      Δ +S/ 20.82 · 4 líneas · residual 0 c · invariante OK
  ✔ CONTEXTO    5 faq · 1 casuística · saneado · U=0.43
  ✔ GENERACIÓN  mock · mock-plantillas-1.0.0 · 23 ms
  ✔ VERIFICA    PASS · 12 ancladas · 0 derivadas · 0 no ancladas · 12 citas
  ✔ RESPUESTA   5 bloques · 3 acciones · LLM · 23 ms · cadena íntegra (11 eventos)

cadena_valida: True | indice_roto: None
```

Sale con `1` si la cadena está rota. Lo mismo por HTTP, con nivel LOA2:
`GET /v1/auditoria/cadena` → `{"cadena_valida": true, "indice_roto": null, "eventos": N,
"hash_ultimo": "..."}`.

### 9.5 La prueba de humo del contenedor — `make smoke`

`docker/smoke.py` es la versión reducida de `probar_e2e.py` que corre **dentro del contenedor**
(4 pasos: salud, token, explicar, auditoría) y es la que hace fallar `make demo`:

```bash
python docker/smoke.py --api http://127.0.0.1:8000 --cuenta C-DEMO-01 --periodo 2026-07 --verbose
```

**Qué debería ver** (salida real, recortada):

```
[3/4] POST /v1/explicar
      veredicto=PASS · totales=12 · ancladas=12 · no ancladas=0
      modo=LLM · modelo=mock-plantillas-1.0.0 · factset=3227801e4fcc · trace=tr-30f6123a6cf2
[4/4] GET /v1/auditoria  (la prueba en el log)
      ╭─ RECIBO CLARO · trace tr-30f6123a6cf2 ──────────────────╮
      │ AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 12 · NO ANCLADAS 0 │
      ╰─────────────────────────────────────────────────────────╯
      cadena_valida=True
==============================================================================
SMOKE OK · explicación verificada, cero cifras sin anclar
==============================================================================
```

Códigos de salida: `0` verificado, `1` la verificación numérica no pasó, `2` la API no respondió.

### 9.6 Estilo

```bash
python -m ruff check .        # make lint
python -m ruff format --check .
python -m ruff format . && python -m ruff check --fix .   # make fmt
```

> **Los dos primeros salen hoy con código `1`, y no es culpa suya.** Medido en este repositorio:
>
> ```
> python -m ruff check .          ->  Found 6 errors.        (exit 1)
> python -m ruff format --check . ->  19 files would be reformatted, 86 already formatted   (exit 1)
> ```
>
> Los 6 avisos son de estilo, no de corrección, y están concentrados en cuatro archivos del núcleo
> de negocio: `packages/core_domain/dinero.py` (2× `UP040`, alias de tipo con la sintaxis antigua),
> `packages/core_domain/esquemas/movimiento.py` y `packages/core_domain/reglas.py` (`RUF022`,
> `__all__` sin ordenar), `packages/core_domain/esquemas/recibo.py` (`SIM102`, dos `if` anidados) y
> `packages/facts_engine/tramos.py` (`RUF007`, `zip` en vez de `itertools.pairwise`). Ninguno
> cambia una cifra ni un comportamiento; la suite y la evaluación pasan con ellos. **Son
> preexistentes**: no los introdujo ningún cambio reciente. `python -m ruff format .` los reformatea
> en un segundo, pero eso toca `packages/facts_engine`, que está congelado a propósito, así que se
> dejan documentados en vez de arreglados. Si necesita que el objetivo de estilo salga en verde en
> CI, límite el alcance: `python -m ruff check apps scripts tests` sí pasa limpio.

### 9.7 La ingesta de un dataset externo — `kaggle_map`

El repositorio trae un **adaptador de ensayo** para datasets tabulares de telecomunicaciones (del
tipo *customer churn*: un cliente por fila, con su cargo mensual, su antigüedad y sus servicios).
No sustituye al dataset sintético: demuestra que la ingesta acepta un origen ajeno, rechaza lo que
no cuadra y **sintetiza el resto declarando qué inventó**. La decisión y sus límites están en
[`docs/datasets_externos.md`](datasets_externos.md).

Se prueba contra el CSV de ejemplo del repositorio, que **lo escribió el equipo** (15 filas, ver
`data/ejemplos_externos/README.md`):

```bash
python -m packages.datagen.mapping.kaggle_map --csv data/ejemplos_externos/telco_ficticio.csv --salida data/externo
```

**Qué debería ver** (salida real, recortada):

```
  ESQUEMA DETECTADO
  columnas del fichero: 13 · reconocidas: 11 · ignoradas: 2
    ignoradas: SeniorCitizen, Dependents
  filas leídas 15 · aceptadas 10 · rechazadas 5
  RECHAZOS (con su motivo):
    RECHAZADO fila 10 (cliente 1010-JJJJJ): antigüedad de 3 meses. Hacen falta al menos 6 …
    fila 11 (cliente 1011-KKKKK): tipo de contrato no mapeado en CONTRATO_MAP: 'Trimestral'. …
    fila 12 (cliente 1012-LLLLL): cargo total inválido: importe vacío
    RECHAZADO fila 13 (cliente 1013-MMMMM): el cargo total 21000 céntimos no cuadra con 20 meses …
    fila 14 (cliente 1001-AAAAA): identificador duplicado. …
  cuentas canónicas producidas: 10 · 60 recibos · 10 órdenes · 20 filas de ground truth
  desviación máxima frente al cargo mensual real: 1 céntimo (S/ 0.01)
  AVISO: Recibos PARCIALMENTE SINTÉTICOS. …
```

**Lo importante son los cinco rechazos**: el adaptador prefiere descartar una fila a inventar un
recibo que no cuadra. Códigos de salida verificados: `0` correcto · `2` nada ingerido o
`--solo-validar` con problemas · `3` error de configuración · `4` el CSV no existe.

Y la comprobación que cierra el argumento — que lo ingerido **atraviesa el motor entero**:

```bash
DATOS_SINTETICOS=data/externo MODO_ALMACENAMIENTO=memoria \
  AUDIT_LOG_PATH=/tmp/rc-8030/eventos.jsonl TELEMETRIA_PATH=/tmp/rc-8030/sondas.jsonl \
  python -m uvicorn apps.api.main:app --port 8030
```

(De nuevo la bitácora aparte: si tiene la instancia del 8000 levantada, dos escritores rompen la
cadena. Ver el [problema 17](#17-cadena_valida-false--la-bitácora-se-rompió).)

Con las 10 cuentas `EXT-…` generadas: `GET /v1/hechos` da `invariante.ok = true` y
`residual_cent = 0`, y `POST /v1/explicar` da `PASS` con `0` cifras sin anclar, en las **10 de
10** (verificado). Es el mismo motor, con datos que no salieron de nuestro generador.

---

## 10. Problemas frecuentes

### 1. `make: command not found` / `make no se reconoce como un comando`

**Causa:** Windows no trae GNU Make, y Git Bash tampoco. Verificado en este equipo.
**Solución:** use los comandos crudos. Equivalencias:

| Objetivo | Comando crudo |
|---|---|
| `make instalar` | `python -m pip install -e ".[dev]"` |
| `make dev` | `python scripts/dev.py` |
| `make probar` | `python scripts/probar_e2e.py --api http://127.0.0.1:8000` |
| `make test` | `python -m pytest` |
| `make eval` | `python -m eval.run_eval --detalle` |
| `make audit` | el `python -c "..."` de la [sección 9.4](#94-la-auditoría--make-audit) |
| `make smoke` | `python docker/smoke.py --api http://127.0.0.1:8000` |
| `make lint` | `python -m ruff check . && python -m ruff format --check .` |
| `make up` / `down` | `docker compose up -d --build --wait` / `docker compose down` |
| `make limpiar-datos` | borre a mano `data/sintetico`, `data/auditoria`, `data/telemetria` |

### 2. `ModuleNotFoundError: No module named 'apps'`

**Síntoma real:**

```
  File "<frozen importlib._bootstrap>", line 1360, in _find_and_load
ModuleNotFoundError: No module named 'apps'
```

**Causa:** está ejecutando `uvicorn` desde un directorio que no es la raíz del repositorio.
Verificado lanzándolo desde `C:\Users\USER`.
**Solución:** `cd` a la raíz (la carpeta que contiene `pyproject.toml`) antes de arrancar, o use
`python scripts/dev.py`, que resuelve las rutas por su cuenta. Si necesita lanzarlo desde otro
sitio: `PYTHONPATH=/ruta/a/recibo-claro python -m uvicorn ...` (PowerShell:
`$env:PYTHONPATH = "C:\ruta\a\recibo-claro"`). Con `pip install -e .` hecho, `pytest` ya resuelve
las rutas solo (`pythonpath = ["."]` en `pyproject.toml`).

### 3. El puerto 8000 está ocupado

**Síntoma real** (arrancando uvicorn a mano):

```
ERROR:    [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8000): solo se
permite un uso de cada dirección de socket (protocolo/dirección de red/puerto)
```

En Linux/macOS: `[Errno 98] Address already in use`.
**Causa:** ya hay una instancia levantada (frecuente: se dejó una de una prueba anterior).
**Solución:** `python scripts/dev.py` **busca otro puerto por su cuenta** y lo avisa (salida real:
`puerto       el 8000 está ocupado; se usa el 8002`, y el resto del banner ya apunta al puerto
nuevo); con `--puerto-fijo` falla en vez de moverse. A mano, use `--port 8010` o cierre la
instancia anterior:

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*uvicorn*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

```bash
# Linux / macOS
lsof -ti:8000 | xargs kill
```

### 4. `404 CUENTA_NO_ENCONTRADA` — no encuentra el dataset

**Síntoma real** (comprobado apuntando `DATOS_SINTETICOS` a una carpeta inexistente):

```json
{"codigo":"CUENTA_NO_ENCONTRADA",
 "detalle":"no hay recibos para la cuenta C-DEMO-01 en BrainyBill",
 "datos":{"cuenta_id":"C-DEMO-01"}}
```

Y `GET /dev/cuentas` devuelve `"total": 0`. Curiosamente `/salud` sigue en 200 y `/v1/catalogo`
devuelve 30 conceptos: el catálogo y el corpus caen a la semilla incorporada.
**Causa:** falta `data/sintetico`, o `DATOS_SINTETICOS` apunta a otro sitio, o arrancó desde otro
directorio (la ruta por defecto es relativa a la raíz del proyecto).
**Solución:**

```bash
python -m packages.datagen.generar --seed 20260804 --clientes 300 --periodo-actual 2026-07 --salida data/sintetico
```

`python scripts/dev.py` lo hace solo. Compruebe después: `GET /dev/cuentas` debe decir
`"total": 300` y traer las tres cuentas de guion (verificado).

### 5. `el paquete 'google-genai' no está instalado`

**Síntoma real:**

```
ErrorProveedor codigo=CONFIGURACION_INVALIDA | el paquete 'google-genai' no está instalado; use LLM_MODE=mock
```

**Causa:** instaló las dependencias a mano y se saltó `google-genai`. Es dependencia declarada del
proyecto, así que `pip install -e ".[dev]"` la trae.
**Solución:** `python -m pip install "google-genai>=1.0,<2.0"` — o, si no va a usar Gemini, no
haga nada: **con `LLM_MODE=mock` el SDK no se importa nunca** (el import es diferido a propósito)
y la batería completa de pruebas pasa sin él. Verificado: 372 pruebas en verde en un entorno donde
`import google.genai` falla.

### 6. Puse `LLM_MODE=gemini` pero sigue respondiendo el mock

**Síntoma:** `gobernanza.modo` dice `PLANTILLA` y `model_version` dice `plantilla-determinista`;
`/salud/preparacion` dice `"llm": {"modo":"gemini", "proveedor":"plantilla-determinista",
"degradado": true}`.
**Causa:** la clave falta, está vacía, es inválida o falta el SDK. En el log de arranque:
`WARNING apps.api.deps | no hay proveedor LLM (CONFIGURACION_INVALIDA): se responderá con
plantilla determinística`.
**Solución:** revise `GEMINI_API_KEY` y `GEMINI_MODEL` (el id debe ser uno vigente según la
documentación de Google) y **reinicie el proceso**: la configuración se lee una sola vez por
proceso (`@lru_cache`), así que exportar la variable con el servidor levantado no hace nada.
No lo confunda con un fallo: la respuesta sigue siendo correcta y verificada.

### 7. El arranque tarda unos segundos de más y avisa de pgvector

**Síntoma real** (con `DATABASE_URL` apuntando a un host que no resuelve):

```
WARNING packages.retriever.vectorial | pgvector no disponible (OperationalError:
(psycopg.OperationalError) failed to resolve host 'db': [Errno 11001] getaddrinfo failed);
el índice vectorial funciona EN MEMORIA (sin persistencia entre procesos)
```

**Causa:** `DATABASE_URL` apunta a `db:5432`, un host que **solo existe dentro de la red de
Docker**. Fuera de Docker no resuelve y se paga el timeout antes de degradar (medido: 3,99 s).
Suele ocurrir por heredar la variable de un `docker compose` o de un `.env` antiguo.
**Solución:** no es un error —la API funciona igual— pero si le molesta la espera, deje
`DATABASE_URL` vacía (es como viene `.env.example`) o fije `MODO_ALMACENAMIENTO=memoria`, que no
intenta conectar en ningún caso. Es lo que hace `scripts/dev.py`. Las tres opciones de
`MODO_ALMACENAMIENTO`: `memoria` (nunca toca PostgreSQL), `postgres` (lo exige y avisa si no
responde) y `auto` (por defecto: PostgreSQL solo si `DATABASE_URL` trae valor).

### 8. `/ui` da 404, o `/docs` sale en blanco

**`/ui` en 404.** Verificado: pasa cuando el proceso se arrancó **antes** de que existiera
`apps/web/estatico`, o cuando se despliega una imagen que no copia `apps/web`. La consola se monta
solo si el directorio existe, y si no existe el código deja la traza
`no se monta /ui: no existe el directorio ...` (es un `INFO` emitido antes de que uvicorn
configure el logging, así que puede no aparecer en la consola).
**Solución:** compruebe que existe `apps/web/estatico/index.html` y **reinicie el proceso**; la
comprobación rápida es `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ui/`, que
debe devolver `200`.

**`/docs` en blanco.** FastAPI sirve Swagger UI desde un CDN — verificado en el HTML que devuelve
`/docs`: `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js`. Sin internet en
el navegador (o con el CDN bloqueado por la red corporativa) la página carga vacía.
**Solución:** use `/ui`, que no depende de la red, o lea el contrato en
`http://127.0.0.1:8000/openapi.json` (50 KB, devuelve 200). `/redoc` también depende de un CDN.

### 9. `403 NIVEL_INSUFICIENTE` al pedir los hechos

**Síntoma real:**

```json
{"codigo":"NIVEL_INSUFICIENTE",
 "detalle":"el recurso exige nivel LOA2 y su sesión está autenticada como LOA1",
 "nivel_requerido":"LOA2","datos":{"nivel_actual":"LOA1"}}
```

**Causa:** pidió el token con `"nivel":"LOA1"` (o `LOA0`). No es un fallo: es la política por
canal. Con LOA1 —el nivel típico de WhatsApp— `POST /v1/explicar` **sí** responde, pero el texto
llega sin un solo dígito (verificado: 0 dígitos en 869 caracteres, los importes sustituidos por
«un monto» y las fechas por «una fecha», más un aviso que invita a autenticarse) y la telemetría
lo marca con `redactado_por_nivel: "LOA1"`.
**Solución:** pida el token con `"nivel":"LOA2"`.

### 10. `403 CUENTA_NO_AUTORIZADA`

**Síntoma real:**

```json
{"codigo":"CUENTA_NO_AUTORIZADA",
 "detalle":"el identificador de cuenta pedido no coincide con el del token; la cuenta se deriva siempre del token",
 "datos":{"cuenta_pedida":"C-DEMO-02","cuenta_del_token":"C-DEMO-01"}}
```

**Causa:** puso `cuenta_id=C-DEMO-02` en la petición usando el token de `C-DEMO-01`.
**Solución:** emita un token para esa cuenta. Es deliberado: **la cuenta sale siempre del token**,
nunca de un parámetro, para que no exista forma de leer el recibo de otro cliente. (La excepción
controlada es `LOA_ASESOR` con `acting_on_behalf_of`, que sí lee la cuenta del cliente atendido y
lo deja escrito en la auditoría.)

### 11. `404 EXPLICACION_NO_ENCONTRADA` al pedir la evidencia

**Síntoma real:**

```json
{"codigo":"EXPLICACION_NO_ENCONTRADA",
 "detalle":"no hay evidencia viva para la explicación tr-000000000000; vuelva a pedir la explicación para regenerarla"}
```

**Causa:** o el `trace_id` está mal copiado, o reinició el servidor. La memoria de conversación
vive **en el proceso** (LRU de 512 turnos), así que un reinicio la vacía — y con `--reload`, un
cambio en el código reinicia el proceso.
**Solución:** vuelva a llamar a `POST /v1/explicar` y use el `trace_id` nuevo
(`telemetria.explicacion_id`, que es el mismo valor que la cabecera `X-Trace-Id`). Con varias
réplicas pasaría lo mismo entre réplicas: está registrado como riesgo R-03 en
[`docs/pendientes.md`](pendientes.md).

### 12. `422 PETICION_INVALIDA` por un campo de más

**Síntoma real:**

```json
{"codigo":"PETICION_INVALIDA","detalle":"la petición no cumple el contrato del endpoint",
 "datos":{"errores":[{"type":"extra_forbidden","loc":["body","sobra"],"msg":"Extra inputs are not permitted"}]}}
```

**Causa:** todos los modelos son `extra="forbid"`. Un campo de más —o mal escrito— es un error, no
se ignora en silencio.
**Solución:** revise el nombre del campo contra `/docs`. Y ojo con este caso contraintuitivo,
verificado: `{"periodo": "julio", "utterance": "por que subio mi recibo"}` **no** da 422 sino
`404 PERIODO_NO_ENCONTRADO` (`"la cuenta C-DEMO-01 no tiene un recibo del periodo julio"`), porque
el periodo es una cadena libre que se busca en el dataset. El formato correcto es `YYYY-MM`.

Un segundo caso contraintuitivo, también verificado: `{"periodo": "julio"}` **sin `utterance`**
devuelve **`200`**, no 404. `utterance` es opcional y por defecto viene vacío; el turno entra por
la compuerta de intención, que clasifica el mensaje como `VACIO` y responde *"No recibí su
consulta. Cuénteme qué necesita…"* con `verificacion_numerica: "NO_APLICA"` y
`model_version: "plantilla-intencion-1.0.0"` **antes de tocar la facturación**. Es la etapa 0 del
pipeline haciendo su trabajo: sin pregunta no se abre el recibo. Si quiere provocar el error del
periodo, mande siempre un `utterance`.

### 13. `401 TOKEN_AUSENTE` o `401 TOKEN_INVALIDO`

**Síntomas reales:**

```json
{"codigo":"TOKEN_AUSENTE","detalle":"esta operación exige autenticación: envíe 'Authorization: Bearer <jwt>'"}
{"codigo":"TOKEN_INVALIDO","detalle":"el token no es válido: Not enough segments"}
{"codigo":"TOKEN_INVALIDO","detalle":"el token no es válido: Invalid header string: ..."}
```

La cola del `detalle` la pone PyJWT y cambia según cómo esté roto el token: `Not enough segments`
si no tiene las tres partes separadas por puntos (el caso típico de una variable vacía),
`Invalid header string: …` si las tiene pero no descodifican, `Signature verification failed` si
lo firmó otro secreto.

**Causa:** olvidó la cabecera, o la variable `$TOKEN` de su terminal está vacía (típico en
PowerShell tras cambiar de ventana), o el token caducó (una hora por defecto, `JWT_TTL_MIN`).
**Solución:** vuelva a emitirlo con `POST /dev/token`. Compruebe que la variable tiene contenido:
`echo $TOKEN` / `$TOKEN.Substring(0,20)`.

### 14. `docker compose` falla al conectar

**Síntoma real:**

```
error during connect: Get "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.46/info":
open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.
```

**Causa:** Docker está instalado pero el demonio no está corriendo (Docker Desktop cerrado).
**Solución:** abra Docker Desktop y espere a que diga *Engine running*; o use la
[ruta A](#3-ruta-a--la-más-rápida-sin-docker-y-sin-claves), que no lo necesita para nada.
En Linux: `sudo systemctl start docker`.

### 15. `jq: command not found`

**Causa:** la documentación de ejemplos usa `jq`, que no viene con Windows.
**Solución:** sustitúyalo por Python, que ya tiene instalado:

```bash
... | python -c "import sys,json;print(json.load(sys.stdin)['gobernanza']['verificacion_numerica'])"
```

En PowerShell no hace falta nada: `Invoke-RestMethod` ya devuelve un objeto
(`$resp.gobernanza.verificacion_numerica`).

### 16. Las pruebas dejan archivos en `data/`

**Síntoma:** `data/auditoria/eventos.jsonl` y `data/telemetria/sondas.jsonl` crecen cada vez que
ejecuta `pytest`, la evaluación o un turno.
**Causa:** es lo previsto: la bitácora es append-only y encadenada, y las pruebas no la redirigen.
No rompe nada (la cadena sigue válida) pero el archivo engorda.
**Solución:** borre `data/auditoria` y `data/telemetria` cuando quiera (`make limpiar-datos`), o
redirija con `AUDIT_LOG_PATH` y `TELEMETRIA_PATH` a una carpeta temporal antes de lanzar la suite.

### 17. `cadena_valida: false` — la bitácora se rompió

**Síntoma real** (`scripts/probar_e2e.py` lo caza):

```
  · auditoría del turno: 11 eventos · veredicto=PASS · cadena_valida=False
  · cadena de hashes (JSONL local): 2413 eventos · íntegra=False · último=eebaea548ecc
HAY FALLOS · 16/19 pasos
```

**Causa:** **dos procesos escribiendo la misma bitácora a la vez.** Pasa si deja levantadas dos
instancias de la API, o si lanza `pytest` o `make eval` con un servidor corriendo: los tres
escriben en `data/auditoria/eventos.jsonl` y sus eventos se intercalan. Diagnóstico exacto del
caso que se reprodujo aquí: dos eventos consecutivos tomaron el **mismo** `hash_previo`.

```bash
python -c "from packages.governance.auditoria import registro_por_defecto as R; r=R(); print(r.verificar_cadena())"
```

```
(False, 2268)     # el índice de la línea donde se rompió
```

**Solución:** deje **un solo escritor** por bitácora. Para las pruebas, redirija con
`AUDIT_LOG_PATH=/ruta/temporal/eventos.jsonl` (y `TELEMETRIA_PATH`), o borre el archivo y vuelva a
empezar: `make limpiar-datos`, o a mano `rm -rf data/auditoria data/telemetria`. Comprobado: con
la bitácora limpia, `probar_e2e.py` vuelve a dar `TODO PASA · 19/19`. Ojo: si la cadena queda
rota, `GET /salud/preparacion` responde **503** con `"listo": false` — es lo correcto, la
integridad del log es parte del producto.

### 18. Añadí una ruta y falla `tests/contract/test_openapi_snapshot.py`

**Causa:** la superficie pública está congelada en un snapshot. Cualquier ruta nueva bajo `/v1/*`
o `/salud*` rompe ese test a propósito. Las rutas bajo `/dev` están excluidas, y `/ui` tampoco
cuenta (es un `Mount` de archivos estáticos, no entra en `/openapi.json`).
**Solución:** si el cambio es intencionado, regenere el snapshot:
`ACTUALIZAR_SNAPSHOTS=1 python -m pytest tests/contract -q`.

---

## 11. Qué NO hace

Para que nadie se sorprenda delante de un cliente o de un jurado.

| No hace | Detalle |
|---|---|
| **No tiene datos reales de Movistar** | Todo el dataset es sintético y determinístico (semilla `20260804`, 300 clientes, 1 800 recibos). Ningún dato personal real ha entrado ni saldrá del sistema. Las cifras de este manual describen ese dataset, no la facturación de nadie |
| **No calcula montos con el modelo de lenguaje** | El modelo redacta. Toda la aritmética —prorrateos, diferencias, atribución de causas, IGV— la hace código Python determinístico en `packages/facts_engine`, y toda cifra del texto se verifica contra el `FactSet` antes de entregarse |
| **No hace ofertas comerciales concretas** | Puede ofrecer la acción `VER_ALTERNATIVAS`, pero **sin plan, sin precio y sin condiciones**: no hay catálogo comercial real en los datos del Desafío 1. Además, el cross-selling está prohibido si el turno se deriva o si el cliente muestra molestia |
| **No resuelve reclamos formales** | Si detecta intención regulatoria —Osiptel, Indecopi, libro de reclamaciones, "cobro indebido"— entrega la explicación y **deriva a un asesor humano** con el contexto cargado. Un asistente de facturación no tramita reclamos |
| **No gestiona bajas ni portabilidades** | Mismo tratamiento: se deriva |
| **No ejecuta acciones sobre la cuenta** | No paga, no da de baja, no cambia de plan, no aplica notas de crédito. Las acciones que devuelve son `INFORMATIVA` o `REVERSIBLE` y las ejecuta el canal, no este servicio |
| **La consola `/ui` no es la App de Movistar** | Es una **consola de demostración** servida por la propia API para enseñar el producto y la gobernanza lado a lado. No hay App, ni Bot Lucía, ni WhatsApp: la integración con los canales reales está fuera del alcance. La respuesta viaja en bloques tipados (`texto`, `kv`, `puente`, `tabla`, `aviso`) listos para que los pinte cualquier canal |
| **No está integrado con el facturador real** | Lee de BrainyBill y de Amdocs a través de un Anti-Corruption Layer. El salto a los sistemas reales es cambiar dos URLs y, si el formato difiere, un solo archivo de mapeo (`packages/datagen/mapping/movistar_map.py`) |
| **No recuerda entre reinicios ni entre réplicas** | La memoria de conversación es un LRU de 512 turnos en el proceso. Afecta a `GET /v1/evidencia` y a `GET /v1/derivacion/{ref}`. Registrado como riesgo R-03 |
| **No garantiza que la narrativa causal sea perfecta en escenarios compuestos** | La aritmética siempre cuadra y la verificación siempre bloquea las invenciones, pero la atribución de causas puede agrupar de más. Caso concreto y plan de corrección: [`docs/pendientes.md`](pendientes.md) §1 |
| **Sus reglas de negocio no están validadas por Movistar** | Todo lo marcado `[POR VALIDAR]` en `db/reglas/rules.yaml` y en `.env.example`: cobro en suspensión, convención de prorrateo, cargo de reconexión, días de gracia |
| **No se ha probado bajo el pico de 3x** | El dimensionamiento está calculado en `docs/arquitectura.md` §6, pero es una estimación del equipo, no una medición |

---

## Para seguir leyendo

| Documento | Qué encontrará |
|---|---|
| [`README.md`](../README.md) | El problema, las decisiones de diseño y la tabla de cumplimiento de la ficha |
| [`ejemplos/curl.md`](../ejemplos/curl.md) | 16 secciones de comandos, incluido un guion de demo de cinco minutos |
| [`docs/arquitectura.md`](arquitectura.md) | Diagramas, el ACL, los niveles por canal, el dimensionamiento |
| [`docs/ADR/`](ADR/) | Por qué el recibo no se vectoriza · por qué céntimos enteros · **por qué el LLM no calcula** · por qué tramos |
| [`docs/pendientes.md`](pendientes.md) | Lo que falta, lo `[POR VALIDAR]` y los riesgos abiertos |
