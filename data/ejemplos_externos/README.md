# `data/ejemplos_externos/` — CSV **inventado por el equipo**, no descargado

## Qué hay aquí

`telco_ficticio.csv` — quince filas escritas a mano por el equipo de recibo-claro para
poder probar `packages/datagen/mapping/kaggle_map.py` **sin conexión, sin cuenta en ninguna
plataforma y sin descargar ningún dataset de terceros**. Diez se aceptan y cinco se rechazan,
cada una por un motivo distinto.

## Qué NO es

- **No procede de ningún dataset público, ni de Kaggle ni de ningún otro repositorio.**
  Ninguna de sus filas se ha copiado, derivado ni muestreado de un fichero descargado.
- **No contiene datos de Movistar**, ni reales ni ficticios entregados por la organización.
- **No contiene datos personales**: los identificadores (`1001-AAAAA`…) son inventados y no
  corresponden a ninguna persona.

Se versiona precisamente porque es original del equipo: no hay licencia de terceros que
respetar ni obligación de confidencialidad que incumplir. Es la única excepción, junto al
`README.md` de `data/`, a la regla de no versionar nada bajo `data/`.

## Por qué reproduce el esquema de los datasets públicos de fuga

Las columnas (`customerID`, `tenure`, `MonthlyCharges`, `TotalCharges`, `Contract`,
`PaymentMethod`, `PhoneService`, `InternetService`, `StreamingTV`, `PaperlessBilling`,
`Churn`) son los **nombres habituales** de los datasets tabulares de *churn* de
telecomunicaciones. Sirven para ejercitar el adaptador contra un esquema que no diseñamos
nosotros. `SeniorCitizen` y `Dependents` están a propósito: son columnas demográficas que el
adaptador **ignora**, y la ejecución lo dice en voz alta.

## Filas de rechazo, puestas a propósito

Cinco de las quince filas están diseñadas para ser rechazadas, cada una por un motivo
distinto, de modo que una sola ejecución demuestre el control de calidad de la ingesta:

| Fila | Cliente | Por qué se rechaza |
|---|---|---|
| 10 | `1010-JJJJJ` | antigüedad de 3 meses: no alcanza para seis recibos |
| 11 | `1011-KKKKK` | tipo de contrato `Trimestral`, ausente de `CONTRATO_MAP` |
| 12 | `1012-LLLLL` | `TotalCharges` en blanco: importe no convertible |
| 13 | `1013-MMMMM` | el cargo total no cuadra con el cargo mensual por la antigüedad |
| 14 | `1001-AAAAA` | identificador de cliente duplicado |

La fila 15 (`1014-CCCCC`) está puesta para que la tanda incluya el escenario insignia del
desafío, el **cambio de plan a mitad de ciclo**: el reparto de escenarios es determinista y
sin ella ninguna de las cuentas aceptadas lo recibía.

## Cómo se prueba

```bash
python -m packages.datagen.mapping.kaggle_map \
    --csv data/ejemplos_externos/telco_ficticio.csv --solo-validar

python -m packages.datagen.mapping.kaggle_map \
    --csv data/ejemplos_externos/telco_ficticio.csv --salida data/externo/
```

> **Aviso metodológico.** Las cuentas que produce ese comando son **parcialmente
> sintéticas**: el cargo mensual y la antigüedad vienen del CSV, pero el desglose por
> concepto, las fechas de ciclo y los movimientos los inventa el equipo. Sirven para
> ejercitar la ingesta, **no** para medir la exactitud del motor. El razonamiento completo
> está en [`docs/datasets_externos.md`](../../docs/datasets_externos.md).
