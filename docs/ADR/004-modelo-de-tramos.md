# ADR 004 — Modelo de tramos en lugar de una fórmula por escenario

**Estado:** aceptada

## Contexto

La ficha exige `[CONFIRMADO-OFICIAL]` demostrar en vivo al menos dos de cinco escenarios —prorrateos, cuota de equipo financiado, reconexión tras suspensión morosa, fin de descuentos y cambio de plan— «todo en ambas modalidades de renta adelantada y vencida».

Con una fórmula por escenario son diez implementaciones, y los casos compuestos (un cambio de plan **y** una suspensión en el mismo ciclo) no se cubren con ninguna.

## Decisión

Un único algoritmo. El ciclo se parte por **todos** los eventos en tramos disjuntos que suman exactamente `D` días:

```
RENTA_ciclo = Σ_j  P_j · (len_j / D) · facturable(e_j)
```

Cada tramo lleva su tarifa vigente y su estado (`ACTIVO` o `SUSPENDIDO`). Sobre esa base se aplican las dos modalidades:

```
VENCIDA     T_k = RENTA_ciclo_k + CONSUMO + CUOTAS + CARGOS − CREDITOS
ADELANTADA  T_k = P_new + (P_new − P_old)·(d_new/D) + CONSUMO + CUOTAS + CARGOS
```

La cuota de equipo financiado se calcula por sistema francés y **nunca se prorratea**.

## Justificación

1. **Los casos compuestos salen gratis.** Dos eventos generan tres tramos; el algoritmo no cambia.
2. **La tabla de tramos *es* la explicación.** «Del 1 al 12 el Plan A, del 13 al 30 el Plan B» es auditable y un analista de facturación la verifica mentalmente.
3. **Hace visible el insight del proyecto:** en renta adelantada, un cambio de plan a mitad de ciclo hace convivir dos rentas en un mismo documento, y el recibo puede subir aunque el plan nuevo sea más barato. Es la respuesta literal al «¿por qué me vino más caro?».

## Consecuencias

- Dos parámetros quedan `[POR VALIDAR]` con Movistar y viven en `rules.yaml`, no enterrados en el código: `COBRO_EN_SUSPENSION` y `CONVENCION_PRORRATEO` (`actual/actual` o `30/360`).
- **Se implementan ambas convenciones.** Ante el jurado se defiende solo: no sabemos cuál usa Movistar, así que el sistema soporta las dos y se declara cuál cierra el invariante al céntimo.
- Los tramos viajan dentro del `FactSet`, de modo que sus días, tarifas y fechas quedan anclados y el modelo puede citarlos sin calcular.
- **Un solo lugar donde equivocarse.** Con diez fórmulas, un error de convención de días se corrige diez veces y se olvida en dos. Con un algoritmo, se corrige en `construir_tramos` y se propaga a los cinco escenarios y a sus combinaciones.
- **Contrapartida asumida:** el algoritmo general es más difícil de leer que una fórmula de dos líneas, y exige tres invariantes que se prueban por separado: los tramos son disjuntos, cubren el ciclo entero, y sus días suman exactamente `D` (`validar_particion`). Los tests recorren meses de 28, 29, 30 y 31 días.
- El motor **descarta** una reconstrucción de tramos que no reproduzca el importe facturado, en lugar de adjuntar una explicación supuesta. Es preferible una explicación sin tabla de tramos a una tabla que no cuadra con lo que el cliente ve en su recibo.

## Alternativas descartadas

| Alternativa | Por qué no |
|---|---|
| Una fórmula por escenario | Diez implementaciones (cinco escenarios × dos modalidades) y ninguna cubre los casos compuestos, que son el 30 % del dataset |
| Un modelo de lenguaje que «razone» el prorrateo | Deja el cálculo en manos de un componente no determinístico y no auditable. Ver ADR 003 |
| Aceptar el importe del facturador sin reconstruirlo | Se podría mostrar el número, pero no explicar de dónde sale. La tabla de tramos es la explicación |

## Referencias

- `packages/facts_engine/tramos.py` — `construir_tramos`, `validar_particion`, `describir_tramos`
- `packages/facts_engine/prorrateo.py` — `renta_del_ciclo`, `total_vencida`, `total_adelantada`, `cronograma_frances`
- `db/reglas/rules.yaml` — `politica.cobro_en_suspension` y `politica.convencion_prorrateo`
- `tests/unit/test_tramos.py`, `test_prorrateo.py`, `test_frances.py`
