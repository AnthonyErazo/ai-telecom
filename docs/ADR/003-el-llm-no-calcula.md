# ADR 003 — El modelo de lenguaje no calcula

**Estado:** aceptada

## Contexto

La ficha exige `[CONFIRMADO-OFICIAL]` dos cosas que parecen tirar en direcciones opuestas: un tono «humano, transparente y horizontal, evitando estructuras robóticas», y «Tasa de Alucinación: cero invenciones financieras **comprobables mediante logs de la terminal**».

Un modelo que redacta libremente sobre un recibo produce el primer objetivo y destruye el segundo: re-calcula deltas, inventa conceptos plausibles y arrastra cifras de los ejemplos que vio.

## Decisión

**El modelo genera la forma. El código inyecta las cifras.**

1. El motor determinístico produce un `FactSet` sellado con su SHA-256.
2. El modelo recibe **solo** ese objeto y el contexto recuperado ya saneado. Sin acceso a base de datos, sin herramientas de cálculo, con prohibición explícita de operar.
3. La salida es estructurada, y los importes se piden como **enteros de céntimos**, lo que vuelve trivial la comprobación.
4. Un verificador **en código** extrae cada cifra del texto y exige que pertenezca al conjunto `ALLOWED`, construido exclusivamente desde el FactSet, o que sea derivable por un **álgebra permitida** cerrada (suma, resta, diferencia de fechas en días, cociente días entre días de ciclo, porcentaje, redondeo), registrando cada derivación.
5. Si falla: un reintento; si vuelve a fallar, **plantilla determinística**. Nunca se emite texto no verificado.

Los tokens llevan **prefijo de magnitud** (`cent:12490` frente a `num:12`). Sin él, los 12 días de un prorrateo anclarían un importe de S/ 0,12.

## Consecuencias

- La métrica comprometida es `TA_respuesta = 0`, no «un porcentaje bajo». Verificada sobre 261 casos y 4 625 afirmaciones numéricas en `make eval`.
- El compromiso es falsable en vivo: `POST /dev/alucinar` fuerza una cifra falsa y el jurado ve el bloqueo y el paso a plantilla.
- El sistema sigue explicando correctamente con el modelo apagado. Eso no es una carencia: es la prueba de que las cifras nunca vinieron del modelo.
- **El coste a escala está acotado, pero no reducido todavía.** Hoy la plantilla es la vía de *fallback*, no la vía principal: en la evaluación, los 261 casos pasan por la ruta del proveedor (`tasa_fallback = 0 %`). La palanca de coste —cachear el **arquetipo narrativo** (`causas + signo + banda de monto + producto + modalidad + verbosidad + versiones`, **sin montos**, precisamente porque el código inyecta las cifras) para servir la mayoría de los turnos sin invocar al modelo— **no está implementada**. Está registrada como propuesta en `docs/arquitectura.md` §10 y en `docs/PROCEDENCIA.md`.
- Contrapartida asumida: el texto es menos libre que el de un modelo sin restricciones. Es el precio de poder prometer cero invenciones.

## Referencias

- `packages/llm_layer/verificador.py` — construcción del conjunto permitido y veredicto
- `packages/llm_layer/generador.py` — orquestación, reintento único y degradación a plantilla
- `packages/llm_layer/prompts/explicar_v1.jinja` — las prohibiciones explícitas del rol
- `tests/golden/test_sin_numeros_no_anclados.py` — la prueba que hace fallar la build
- `docs/arquitectura.md` §8 — secuencia completa de verificación
