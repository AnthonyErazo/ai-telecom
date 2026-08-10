# El modelo más adecuado para este problema

> **Estado de las cifras.** Los precios son los publicados por Anthropic y verificados en
> agosto de 2026. Los costes por unidad son **cálculo propio** sobre supuestos declarados en
> §4. Las latencias de Gemini son **medidas nuestras**; las de Claude **no se han medido
> todavía** y están marcadas como tales. Las comparativas de español y de salida estructurada
> son de terceros, citadas al final.

---

## 1. El problema, planteado sin adornos

Un cliente recibe un recibo más caro que el mes pasado y quiere saber por qué. La respuesta
correcta casi nunca es un solo motivo: suele ser la suma de varias causas que se compensan
entre sí — se venció un descuento, cambió de plan, hubo prorrateo por un cambio a mitad de
ciclo, entró una cuota de equipo.

Eso convierte el problema en dos problemas distintos, y confundirlos es el error de diseño
más caro que se puede cometer aquí:

| | Naturaleza | Quién debe resolverlo |
|---|---|---|
| **Cuánto y por qué cambió** | Aritmética determinística sobre el recibo | Un motor de cálculo |
| **Decírselo a una persona** | Lenguaje, registro, empatía | Un modelo generativo |

Un modelo generativo que además calcula produce respuestas plausibles y a veces falsas. En
facturación, una cifra falsa no es un error de estilo: es un reclamo, y probablemente una
segunda llamada más cara que la primera.

**Por eso el motor calcula, el modelo redacta, y un verificador comprueba que el modelo no
inventó nada.** Este documento decide únicamente la tercera pieza: qué modelo redacta.

---

## 2. Por qué esta decisión es más pequeña de lo que parece

El modelo **no razona**. Cuando le llega el turno, los tramos, el prorrateo, el diff por
concepto y la atribución causal ya están calculados y conciliados. Recibe dos encargos:

| Encargo | ¿Lleva cifras? | Riesgo si el modelo se equivoca | Qué se le exige de verdad |
|---|---|---|---|
| Explicar el recibo | Sí, todas verificadas contra el `FactSet` | **Nulo** — el verificador bloquea y cae a plantilla | JSON fiable + redacción clara |
| Turno conversacional | **Ninguna** — cualquier dígito se bloquea | **Nulo** | Solo registro y naturalidad |

De ahí se sigue algo contraintuitivo: **la inteligencia bruta no es el criterio.** Un modelo
capaz de resolver problemas de olimpiada no aporta nada, porque la aritmética ya está hecha y
comprobada. Lo que se compra es **prosa en español peruano** y **formato fiable**.

Hay una segunda razón para relativizar la elección, y es la más importante:

> Una evaluación independiente de salida estructurada de 2026 encontró que **todos los modelos
> producen JSON casi perfecto, y aun así una fracción considerable de los valores dentro de ese
> JSON son incorrectos** — un fallo silencioso que se propaga por la tubería sin avisar.

Cumplir el esquema no es decir la verdad. Ningún modelo resuelve eso; lo resuelve el
verificador, que se ejecuta sobre el texto final lo haya escrito quien lo haya escrito. Esa es
la razón por la que la métrica de cero cifras sin respaldo se puede medir **sin modelo alguno**,
en modo determinístico: la garantía no depende del fabricante.

---

## 3. Los cuatro criterios que sí discriminan

Ordenados por cuánto pesan **en este problema concreto**:

1. **Registro natural en español peruano.** Es lo único que el motor no puede garantizar por
   construcción, y es lo que percibe el cliente. Peso alto.
2. **Fiabilidad de la salida estructurada.** Importa, pero con red debajo: un fallo de esquema
   degrada a plantilla determinística, no produce una respuesta incorrecta.
3. **Latencia.** En un chat, la latencia *es* la experiencia. Dos segundos de diferencia se
   notan más que un adjetivo mejor elegido.
4. **Gobernanza del dato y despliegue.** No decide hoy; decide en producción.

### Lo que dicen los rankings, y por qué no bastan

Conviene ser exacto, porque aquí es fácil pasarse de rotundo. **Ningún fabricante barre.** Los
rankings de agosto de 2026 están repartidos:

| Benchmark | Quién lidera |
|---|---|
| BenchLM Multilingüe | **Qwen3.7 Max (100 %)** — Claude Opus 4.5 2.º con 82,9 % |
| Multilingual MMLU | **o3-mini (OpenAI)**, 0,807 |
| MMMLU | Claude Mythos Preview, 0,927 |
| SWE-bench Multilingual | Claude Mythos Preview, 0,873 |

Lo más llamativo: **el que lidera el ranking multilingüe general es un modelo de pesos abiertos**,
no uno propietario. Eso debilita el prejuicio de que lo autoalojado es necesariamente peor.

Sobre salida estructurada, GPT-4o encabeza el cumplimiento de esquema (99,9 %) con Claude muy
cerca (99,8 % vía *tool use*) — medio punto que, por lo dicho en §2, es ruido frente a la clase
de error que el verificador ya cubre.

**Y ahora la advertencia que más importa: ninguno de esos benchmarks mide nuestra tarea.**
MMLU, MGSM y SWE-bench miden conocimiento y razonamiento *a través* del español. Aquí no hace
falta que el modelo sepa nada ni razone —el motor ya calculó y el verificador ya comprobó—:
hace falta que **tres frases suenen a una persona de Lima**. Eso no lo mide ningún ranking
público, y por eso este documento termina en §9 pidiendo una medición propia en vez de citar
una tabla ajena.

Existe además una comparativa que sitúa a la familia Claude por delante en naturalidad del
español y con menos anglicismos, pero **su término de comparación es GPT-4**, un modelo de 2024.
Como evidencia para decidir en 2026 es débil, y se declara aquí como tal.

---

## 4. Precios verificados — agosto de 2026

| Modelo | Entrada / 1M tokens | Salida / 1M tokens | Contexto |
|---|---|---|---|
| Claude Opus 5 | 5,00 USD | 25,00 USD | 1M |
| Claude Sonnet 5 | 3,00 USD | 15,00 USD | 1M |
| Claude Sonnet 5 *(precio de lanzamiento, hasta 31-08-2026)* | **2,00 USD** | **10,00 USD** | 1M |
| Claude Haiku 4.5 | 1,00 USD | 5,00 USD | 200K |

La **caché de prompt** cambia la aritmética: el texto estable (instrucciones, reglas de tono,
ejemplos) se cobra a ~0,1× en las lecturas siguientes. Como aquí el prompt de sistema es fijo y
lo único que varía es el `FactSet`, la caché aplica casi siempre.

### Supuestos del cálculo

Declarados para que cualquiera pueda rehacerlos con otros números:

- **200.000 explicaciones/mes** — es el volumen de llamadas al 104 por esta consulta.
- **~1.750 tokens de entrada** por explicación (instrucciones + `FactSet` serializado) y
  **~250 de salida**, de los cuales ~1.000 de entrada son estables y cachean.
- **600.000 turnos conversacionales/mes** — supuesto de 3 por sesión. **Este es el supuesto
  más frágil del documento** y el que más mueve el resultado (véase §5).
- ~800 tokens de entrada y ~80 de salida por turno conversacional, ~600 estables.

---

## 5. Lo que cuesta de verdad

### A) Camino de explicación — 200.000/mes

| Modelo | Sin caché | Con caché | **Por explicación** |
|---|---|---|---|
| Claude Opus 5 | 3.000 USD | 2.100 USD | 1,05 ¢ |
| Claude Sonnet 5 | 1.800 USD | 1.260 USD | 0,63 ¢ |
| Claude Sonnet 5 *(lanzamiento)* | 1.200 USD | 840 USD | 0,42 ¢ |
| Claude Haiku 4.5 | 600 USD | 420 USD | **0,21 ¢** |

### B) Camino conversacional — 600.000/mes

| Modelo | Sin caché | Con caché | **Por turno** |
|---|---|---|---|
| Claude Opus 5 | 3.600 USD | 1.980 USD | 0,33 ¢ |
| Claude Sonnet 5 | 2.160 USD | 1.188 USD | 0,20 ¢ |
| Claude Haiku 4.5 | 720 USD | 396 USD | **0,07 ¢** |

### El número honesto

**Menos de un céntimo de dólar por explicarle a una persona su recibo.** Esa es la cifra que
hay que mirar. El total mensual suena grande y no informa; el coste unitario sí.

Deliberadamente **no comparo contra el coste de una llamada atendida por una persona**: no
existe cifra pública de lo que le cuesta al operador, y ponerla sería inventarla. Lo que sí es
estructuralmente cierto y no necesita fuente: cualquier atención humana de esa llamada —
salario, puesto, supervisión, telefonía— está órdenes de magnitud por encima de 0,21 céntimos.
La comparación la debe hacer el operador con sus propios números, que son los únicos válidos.

### Un resultado que invierte la intuición

Lo natural es pensar «pongo el modelo barato en la tarea aburrida y el caro en la conversación».
Los números dicen otra cosa:

| Configuración | Coste/mes |
|---|---|
| Todo Claude Haiku 4.5 | **816 USD** |
| Enrutado: Haiku explica + Sonnet 5 conversa | 1.608 USD |
| Enrutado: Sonnet 5 explica + Haiku conversa | 1.656 USD |
| Todo Claude Sonnet 5 | 2.448 USD |

**El camino conversacional domina el coste porque es 3× más voluminoso**, aunque cada turno
sea más barato. Enrutar por tarea no ahorra lo que parece: el ahorro se lo come el volumen del
lado conversacional.

Y esto depende por completo del supuesto de 3 turnos por sesión. **Si la mezcla real de tráfico
es distinta, la tabla se reordena.** Es la primera cosa que hay que medir con tráfico real
antes de comprometer una arquitectura de costes.

---

## 6. La decisión, según lo que se priorice

Aquí está el núcleo del documento. No hay un modelo mejor en abstracto: hay un modelo mejor
**para cada prioridad declarada**.

### Si se prioriza la humanización por encima de todo

→ **Claude Opus 5** (5/25 USD) · 1,05 ¢ por explicación

**Por qué:** es el modelo de mayor capacidad de la línea, y la familia Claude es la que produce
español más natural y con menos anglicismos. En prosa larga y en matiz emocional —un cliente
molesto porque le subió el recibo— es donde se nota la diferencia de nivel.

**Condiciones para que sea la elección correcta:**
- Que la naturalidad sea un objetivo medido y no una impresión.
- Que se tolere ~2.100 USD/mes en el camino de explicación.
- Que la latencia adicional no rompa la experiencia de chat. **No medida.**

### Si se prioriza la velocidad y el coste

→ **Claude Haiku 4.5** (1/5 USD) · 0,21 ¢ por explicación · **816 USD/mes en todo el sistema**

**Por qué:** un modelo más pequeño responde antes, y en un chat eso es calidad percibida. Es
3× más barato que Sonnet 5 y **6× más barato en el sistema completo**. Para el camino de
explicación —tres frases redactadas desde un `FactSet` con cada cifra ya verificada— es
razonable pensar que basta.

**Condiciones para que sea la elección correcta:**
- Que su español conserve registro natural en textos cortos. **No medido — es el riesgo.**
- Que la ventana de 200K baste, que sobra holgadamente aquí.
- Que se acepte la posibilidad de prosa algo más plana en los turnos conversacionales.

### Si se priorizan velocidad **y** humanización a la vez

→ **Claude Sonnet 5** (3/15 USD, lanzamiento 2/10 hasta el 31-08) · 0,63 ¢ · 2.448 USD/mes

**Por qué, punto por punto:**
1. Es la mejor relación conocida entre calidad de prosa en español y latencia de la gama.
2. Cumple esquema al 99,8 % vía *tool use*, con el verificador cubriendo lo que ningún modelo
   cubre.
3. `effort: low` permite bajar la latencia en los turnos cortos sin cambiar de modelo.
4. Ventana de 1M, que elimina cualquier preocupación de contexto.
5. Hasta el 31 de agosto de 2026 cuesta lo mismo que costaría un modelo de gama inferior hace
   un año.

**Condiciones:** que se acepte un coste intermedio y que no haya requisito de residencia de
datos que obligue a otra cosa.

### Si se prioriza el control y la confidencialidad

→ **Modelo de pesos abiertos autoalojado**, o **Claude sobre Bedrock / Vertex / Foundry** con
retención cero.

**Por qué:** cuando existe una obligación de confidencialidad de años sobre los datos del
operador, la postura que mejor encaja es que el dato no salga. El coste marginal desaparece y
no hay cuota que agotar.

**Candidatos concretos**, porque «un modelo local» sin nombre no es una recomendación:

| Modelo | VRAM | Nota |
|---|---|---|
| **Qwen3-235B-A22B** | 40 GB+ | 78,4 medio en español; tope de la gama abierta |
| **Qwen3-72B** | 40 GB+ | 89,2 % en español según otra medición |
| **Qwen3.6-27B** | 24 GB | El punto dulce para una sola GPU |
| **Qwen3-14B** | ~16 GB | Recomendado para español; **el más realista aquí** |
| **Llama-3.1-8B-Instruct** | 8 GB | El más ligero razonable |
| **Gemma 4** | 8 GB | Portátiles y borde |
| **DeepSeek-R1** | variable | Orientado a razonamiento — **irrelevante para este problema**: no hay nada que razonar |

Regla práctica de hardware: 8 GB para la clase 7-8B, **24 GB para la clase 30B**, 40 GB+ para
70B sin cuantizar agresivamente.

Para este problema miraría **Qwen3-14B o Qwen3.6-27B**: caben en hardware realista y la familia
Qwen es la que puntúa alto en español. Que DeepSeek-R1 sea excelente razonando no aporta nada
aquí, y es un buen recordatorio de que el modelo «mejor» en abstracto no es el mejor para una
tarea concreta.

**Se conectan por el mismo adaptador.** El catálogo de LangChain incluye `ollama` y
`huggingface`, así que pasar de una API a un modelo en la propia máquina es cambiar una cadena
—`LLM_LANGCHAIN_MODELO=ollama:qwen3:14b`— sin clave de API y sin tocar código.

**Condiciones — y son duras:**
- Se necesita GPU para responder en tiempo de chat. En CPU, lo que tarda dos segundos tarda
  decenas.
- El seguimiento de instrucciones en **salida estructurada y en español** es más frágil en
  modelos abiertos pequeños, y aquí la salida estructurada es lo que hace verificable el texto.
- El coste deja de ser variable y pasa a ser infraestructura fija. Con estos volúmenes,
  **mantener GPUs cuesta más que 816 USD/mes**: el argumento económico a favor de lo
  autoalojado no se sostiene, solo el argumento de confidencialidad.

La vía intermedia —Claude sobre la nube privada del operador con retención cero— conserva la
calidad y satisface la confidencialidad sin infraestructura propia. **Es probablemente el
destino correcto en producción.**

---

## 7. Recomendación

**Claude Sonnet 5 como modelo único.**

Es la respuesta a «velocidad y humanización a la vez», que es lo que este problema pide: el
cliente tiene que percibir que le habla alguien, y tiene que percibirlo rápido. El coste
intermedio compra exactamente eso.

**Con dos matices que importan más que la elección:**

1. **El enrutado por tarea no es la optimización obvia.** Los números de §5 muestran que
   ahorra menos de lo que intuitivamente parece y añade una pieza móvil. Antes de introducirlo,
   medir la mezcla real de tráfico.
2. **Si el coste llega a ser un problema, el primer paso no es cambiar de modelo: es la caché
   de prompt.** Reduce el gasto un 30-45 % sin tocar una línea de lógica ni degradar nada.

---

## 8. Qué invalidaría esta recomendación

Un documento de arquitectura que no dice cómo se equivoca no sirve. Esto la tumbaría:

| Condición | Qué pasaría entonces |
|---|---|
| El operador tiene un acuerdo marco con otro proveedor | La elección técnica es irrelevante; manda el contrato |
| Existe requisito de residencia de datos en Perú | Obliga a nube privada o autoalojado, no a API pública |
| Haiku 4.5 resulta indistinguible en español | Se cae el argumento de Sonnet 5: 6× más barato gana |
| La mezcla real es 1 turno conversacional por sesión, no 3 | El enrutado por tarea sí ahorra, y pasa a ser recomendable |
| La latencia de Sonnet 5 supera ~2,5 s en explicación | Deja de cumplir «velocidad»; Haiku pasa a ser la elección |

---

## 9. Lo que todavía no está medido

Con nombre y apellidos, para que nadie construya sobre arena:

- **Latencia real de cualquier modelo Claude sobre nuestro corpus.** Lo único medido en este
  sistema es Gemini: 1,46 s en turno conversacional y 2,2–3,1 s en explicación de recibo.
- **Si Haiku 4.5 escribe peor español peruano que Sonnet 5 en textos de tres frases.** No hay
  comparativa publicada de eso, y es exactamente la pregunta que decide entre 816 y 2.448
  USD/mes.
- **La mezcla real de tráfico** entre explicaciones y turnos conversacionales.

Las tres se resuelven con el mismo experimento: mismo `FactSet`, los modelos en paralelo,
latencia y texto lado a lado sobre los casos de evaluación. Es trabajo de una hora, y sustituye
tres suposiciones por tres medidas.

---

## 10. Por qué el cambio es barato

El proveedor vive detrás de un contrato de **un solo método**:

```python
class ProveedorLLM(Protocol):
    nombre: str
    def completar(self, prompt: str, esquema: dict, timeout_s: float = 4.0) -> dict: ...
```

El acoplamiento se puede contar:

| Dónde | Líneas que conocen al proveedor |
|---|---|
| Implementación concreta del proveedor | ~300, todas aisladas |
| Motor de cálculo (tramos, prorrateo, diff, atribución) | **0** |
| Verificador numérico | **0** |
| Configuración | ~6 |

Cambiar de fabricante es escribir un fichero de unas trescientas líneas y una variable de
entorno. No es una promesa de diseño: **ya hay tres implementaciones conviviendo** y se eligen
con `LLM_MODE`.

Esa es la razón por la que este documento puede permitirse recomendar sin dramatismo. Si la
medición contradice la recomendación, cambiarla cuesta una tarde — y si el operador impone otro
proveedor por contrato, el sistema lo absorbe sin tocar la lógica de negocio ni la garantía de
que ninguna cifra llega al cliente sin respaldo.

---

## 11. Cómo se conecta el modelo: SDK directo frente a LangChain

Esta sección existe porque la pregunta se repite y la respuesta corta —*«LangChain es un
adaptador»*— no basta para entenderlo.

### Qué NO es LangChain

Tres confusiones habituales, las tres falsas:

| Se cree que… | En realidad |
|---|---|
| «LangChain es otro modelo» | No genera texto. No tiene pesos. No sabe español |
| «LangChain es el respaldo si Gemini falla» | El respaldo es la **plantilla determinística**. LangChain no rescata nada |
| «Gemini procesa y se lo pasa a LangChain» | Va **antes**, no después. LangChain llama a Gemini, no al revés |

**LangChain es un traductor de vocabulario.** Nada más, y por eso su valor es real pero
limitado.

### Las dos llamadas, lado a lado

Esto es el código de este repositorio, no un ejemplo inventado. Ambas piden lo mismo:
*«redacta esto y devuélvemelo con esta forma exacta»*.

**Por el SDK de Google** (`providers/gemini.py`) — vocabulario de Google:

```python
comun = {
    "temperature": 0.0,
    "candidate_count": 1,
    "response_mime_type": "application/json",
    "max_output_tokens": 8192,
}
config = self._types.GenerateContentConfig(**comun, response_json_schema=esquema)
```

**Por LangChain** (`providers/langchain_.py`) — vocabulario genérico:

```python
modelo.with_structured_output(esquema)
```

Misma petición, mismo resultado, distinto idioma. `with_structured_output` averigua a qué
fabricante está hablando y construye por debajo el `GenerateContentConfig` de arriba.

### Y entonces, ¿para qué sirve?

Para **una sola cosa**, que se ve cambiando de fabricante:

```bash
LLM_LANGCHAIN_MODELO=google_genai:gemini-2.5-flash   # Google
LLM_LANGCHAIN_MODELO=anthropic:claude-sonnet-5       # Anthropic
LLM_LANGCHAIN_MODELO=openai:gpt-5.4                  # OpenAI
LLM_LANGCHAIN_MODELO=ollama:qwen3:14b                # local, sin clave
```

Con el SDK directo, cada uno de esos saltos son ~300 líneas nuevas. Con el adaptador, es
una cadena de texto. **Eso es todo lo que compra, y no es poco.**

### Por qué ya está instalado sin haberlo decidido

```
langchain-core → Required-by: langgraph, langgraph-checkpoint,
                              langgraph-prebuilt, langgraph-sdk
```

`langchain-core` entró como dependencia obligatoria de **LangGraph**, que es quien
orquesta el flujo (`ORQUESTADOR=grafo` por defecto). El `LangChainProvider` reutiliza una
biblioteca que ya estaba. **Coste marginal en dependencias: cero.**

Lo que sí falta es la integración concreta de cada fabricante —`langchain-google-genai`,
`langchain-anthropic`, `langchain-ollama`—, que son `pip install` independientes.

### Las dos rutas a Gemini, y qué cambia de verdad

```
LLM_MODE=gemini      nuestro código → GeminiProvider ───────────► API Google → Gemini
LLM_MODE=langchain   nuestro código → LangChainProvider → LC ───► API Google → Gemini
```

| | SDK directo | Vía LangChain |
|---|---|---|
| Resultado para el cliente | Idéntico | Idéntico |
| Verificador y motor de hechos | No se enteran | No se enteran |
| Capas entre nosotros y el fallo | 1 | 2 |
| Estado | **Depurado** — tres fallos de integración encontrados y resueltos | Escrito y con pruebas, pero **no ejercitado en marcha** |
| Cambiar de fabricante | ~300 líneas | Una cadena |

### El argumento a favor de usarlo siempre, que es mejor de lo que parece

Hay una razón sólida para enrutar Gemini por LangChain aunque hoy no haga falta:
**el código dormido se pudre.** Un camino que nadie ejecuta es un camino que nadie sabe si
funciona. Si la portabilidad es la promesa que le hacemos a Integratel —*«cambiar de
fabricante es una variable de entorno»*—, esa promesa se sostiene mucho mejor si la ruta
portable es la que se usa a diario, no la que duerme.

El argumento en contra es de calendario, no de diseño: el `GeminiProvider` cargó con los
tres fallos de integración que documenta §6.7.2 de `FUNDAMENTACION.md` y hoy está
depurado. Sustituir un camino probado por uno sin ejercitar, a pocos días de una
demostración, es riesgo sin recompensa **para esa demostración**.

### La medición, que zanjó la discusión

Se instaló `langchain-google-genai` y se pasaron los mismos casos por las dos rutas, con
el mismo modelo (`gemini-2.5-flash`) y el mismo esquema. Tres rondas por ruta y método:

| Método de salida estructurada | SDK directo | vía LangChain | Factor |
|---|---|---|---|
| por defecto de la integración | **1,51 s** | 5,91 s | 3,9× |
| `json_schema` | **1,65 s** | 6,14 s | 3,7× |
| `json_mode` | *(cuota agotada)* | *(timeout a 30 s)* | — |

**Lo que sí coincidió**, y es lo que importaba comprobar:

```
SDK directo     causas=3 · montos=[-3226, 318, 4990]
vía LangChain   causas=3 · montos=[-3226, 318, 4990]
```

Cifras idénticas y texto comparable en registro y naturalidad. El adaptador **funciona**;
no es una promesa sin ejercitar.

**Conclusión: no se consolida en LangChain.** No es un problema de configuración —los tres
métodos dan el mismo resultado—: sobre `langchain-google-genai` el camino cuesta unos
**4,5 segundos más** en la explicación de recibo, y eso en un chat es descalificatorio. El
argumento del «código dormido se pudre» es bueno, pero no compra cuatro segundos.

Decisión resultante, y por qué cada pieza se queda donde está:

| Pieza | Destino | Motivo |
|---|---|---|
| `GeminiProvider` | **Se queda como ruta en marcha** | 4× más rápido y ya depurado |
| `LangChainProvider` | **Se queda, sin ejercitar** | Es la portabilidad real: mismas cifras, otro fabricante a una cadena de distancia |
| `langchain-google-genai` | **Desinstalado** | Se instaló solo para medir; sin uso, sobra |
| `langchain-core` | Se queda | Dependencia obligatoria de LangGraph; no es decisión nuestra |

La ruta portable deja de ser una afirmación y pasa a ser un dato: se midió, produce las
mismas cifras y cuesta 4,5 s más. Quien la necesite —Integratel con otro fabricante—
sabe exactamente qué obtiene y qué paga.

---

## Fuentes

Precios: documentación oficial de Anthropic, verificados en agosto de 2026.

Comparativas de terceros consultadas:

- [Modelos LLM en español: comparativa actualizada (2026)](https://www.revistainteligenciaartificial.com/modelos-llm-espanol-comparativa/) — naturalidad del español y anglicismos
- [The Structured Output Benchmark (arXiv)](https://arxiv.org/html/2604.25359v1) — calidad de la salida estructurada
- [Best LLM for JSON Output: Structured Data Generation Compared](https://deploybase.ai/articles/best-llm-for-json-output-structured-data-generation) — cumplimiento de esquema
- [Evaluating LLM Structured Output Modes (2026)](https://futureagi.com/blog/evaluating-llm-structured-output-modes-2026/) — valores incorrectos dentro de JSON válido
