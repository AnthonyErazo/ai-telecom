# ADR 001 — El recibo no se vectoriza

**Estado:** aceptada

## Contexto

La ficha del Desafío 1 exige `[CONFIRMADO-OFICIAL]` un asistente «sustentado en una arquitectura RAG» y, en el mismo documento, «respuestas limitadas estrictamente a la base de datos de facturación provista, para garantizar 0 % de alucinaciones financieras».

La lectura ingenua —meter recibos y líneas en un índice vectorial y recuperar por similitud— cumple la primera exigencia y hace imposible la segunda.

## Decisión

Se separan los datos por su naturaleza, no por comodidad de implementación:

| Naturaleza | Acceso | Vectores |
|---|---|---|
| Recibos y líneas (**transaccional**) | `SELECT` + full outer join por `concepto_id` | **Nunca** |
| Catálogo de conceptos (**definiciones**) | lookup por clave | secundario |
| FAQs (**lenguaje del cliente**) | híbrido BM25 + vectorial con RRF | sí |
| Casuísticas (**estructura narrativa**) | vectorial por firma causal | sí |

La propia ficha respalda la separación: los datos llegan «listos para su vectorización **o** procesamiento tabular».

## Justificación

1. **La recuperación semántica es aproximada por diseño; un recibo es aritmética exacta.** Buscar «el cargo de reconexión» por similitud coseno puede traer la línea del mes equivocado, dos líneas parecidas o ninguna.
2. **Un vector no se resta de otro para dar S/ 20,00.** La variación mes a mes es un diff de conjuntos, no una tarea de lenguaje.
3. **Sin recuperación exhaustiva no hay invariante.** Si el retriever devuelve *k* líneas, el residual deja de poder cerrarse y se pierde el garante del 0 %.

## Consecuencias

- El RAG aporta **lenguaje y estructura**; jamás números.
- `packages/retriever/saneador.py` es obligatorio: sustituye toda cifra de un documento recuperado por un marcador genérico antes del prompt.
- Aunque una cifra sobreviviera, el verificador la marcaría como no anclada, porque `ALLOWED` se construye **solo** desde el FactSet.
- Ante el jurado, saber dónde *no* aplicar una herramienta es la defensa: el argumento cierra el requisito de RAG y el de 0 % de alucinaciones a la vez.
- **Consecuencia operativa medible:** el índice vectorial guarda **95 documentos de conocimiento y no crece con los clientes**. Vectorizar 5 millones de recibos al mes con seis ciclos de historia sería un índice de decenas de millones de vectores, a reconstruir cada ciclo, con su coste de *embeddings* y su ventana de inconsistencia.
- **Menos superficie de datos personales.** Los importes de facturación son datos personales; no se replican en un segundo almacén con otro control de acceso y otro ciclo de borrado.
- **Contrapartida asumida:** no se puede preguntar al recibo en lenguaje libre y arbitrario. Solo se responden las preguntas que el `FactSet` sabe contestar; fuera de ese perímetro el sistema deriva a un asesor en vez de improvisar.

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| Vectorizar los recibos y hacer RAG puro | No permite conciliar; abre la puerta a mezclar periodos o clientes; explota en coste |
| Vectorizar un resumen textual del recibo | El mismo problema disfrazado: la cifra sigue viniendo de una recuperación aproximada |
| Dar al modelo acceso a la base de datos mediante herramientas | Traslada al modelo la responsabilidad de la exactitud, que es justo lo que rechaza el ADR 003 |

## Referencias

- `packages/retriever/corpus.py`, `hibrido.py`, `saneador.py`
- `packages/facts_engine/diff.py`, `invariante.py`
- `db/esquema.sql` — solo las tablas de corpus (`faq`, `casuistica`) llevan columna `vector`
- `docs/arquitectura.md` §7
