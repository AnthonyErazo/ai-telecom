# ADR 002 — Todo monto es un entero de céntimos

**Estado:** aceptada

## Contexto

El sistema se sostiene sobre un invariante: `Δ_total = Σ Δ_líneas`, con residual cero. Ese invariante es simultáneamente el garante del 0 % de alucinaciones, la señal del umbral de incomprensión y el indicador que se demuestra en vivo.

Con aritmética de punto flotante, un residual de `0.0000001` lo rompe. Y romperlo no es un detalle estético: el sistema deja de explicar y deriva.

## Decisión

- Todo importe es `int` en céntimos, con sufijo `_cent`.
- El reparto de decimales usa **mayor resto**: `c_i = floor(x_i)`, y el sobrante se asigna de a un céntimo a las mayores partes fraccionarias. La suma de líneas es idénticamente igual al total, siempre.
- El prorrateo se calcula en enteros con redondeo bancario.
- La participación porcentual de cada causa se guarda en **puntos básicos** (`participacion_bp`), no como fracción, para no introducir punto flotante ni siquiera en un reparto.
- `tests/unit/test_sin_float.py` recorre `packages/facts_engine/` y **falla la build** si encuentra punto flotante en lógica monetaria.

## Consecuencias

- Toda comparación monetaria es exacta; la tolerancia del invariante es de ±1 céntimo y existe únicamente para absorber el redondeo del IGV.
- El verificador puede comparar por igualdad estricta en lugar de por épsilon, lo que elimina una clase entera de falsos negativos.
- El formateo a texto ocurre en un solo lugar (`core_domain/dinero.py`), de modo que las cadenas que el verificador debe reconocer son un conjunto cerrado y conocido.
- **Contrapartida asumida:** hay que pensar en céntimos todo el rato, y los cálculos con tasas —el sistema francés del equipo financiado— exigen `Fraction` o `Decimal` como tipo intermedio, nunca `float`. `cuota_equipo_financiado` **rechaza un `float` con `TypeError`** en lugar de aceptarlo y redondear en silencio.
- La conversión desde el exterior está centralizada en `dinero.a_centimos`, que acepta los formatos peruanos habituales (`"S/ 1,234.50"`, `"1.234,50"`, `"124,90"`, `"(12.30)"` para negativos). Si el sistema real entregara soles decimales, se activa el interruptor `IMPORTES_EN_CENTIMOS = False` del ACL y todo pasa por ahí: un solo punto de entrada, un solo sitio donde equivocarse.

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| `float` con tolerancia por épsilon | Convierte cada comparación en una decisión con umbral y hace irreproducible el residual |
| `Decimal` en los campos del modelo | Correcto aritméticamente, pero serializa a texto, complica el `sha256` canónico del `FactSet` y sigue permitiendo redondeos implícitos distintos según el contexto |
| Enteros en soles, decimales aparte | Reintroduce el problema con más piezas |

## Referencias

- `packages/core_domain/dinero.py` — `a_centimos`, `repartir_mayor_resto`, `redondear_banca`
- `packages/facts_engine/prorrateo.py` — prorrateo y sistema francés en enteros
- `tests/unit/test_dinero.py`, `tests/unit/test_sin_float.py`
- `tests/propiedad/test_invariante.py` — Hypothesis sobre `Σ deltas == Δ total`
