# Datasets externos: por qué no usamos ninguno como fuente, y qué construimos en su lugar

Hackathon AI Telecom Challenge 2026 · Desafío 1 · Integratel Perú S.A.A. (Movistar) + Universidad de Lima.

Etiquetado: `[CONFIRMADO-OFICIAL]` cita literal de BASES o de la ficha · `[SUPUESTO]` ·
`[PROPUESTA]` · `[POR VALIDAR]`. Nada marcado `[PROPUESTA]` o `[SUPUESTO]` debe leerse como
dato de Movistar.

> **Resumen en una línea.** No usamos ningún dataset público como fuente de recibos, porque
> los que existen no tienen recibos; construimos un **adaptador de ensayo** que ingiere el
> esquema típico de esos datasets para demostrar que el *anti-corruption layer* funciona
> contra un esquema que no diseñamos nosotros.

---

## 1. La decisión

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

## 2. Lo que sí construimos: el adaptador de ensayo

`packages/datagen/mapping/kaggle_map.py` — hermano gemelo de
`packages/datagen/mapping/movistar_map.py`, con su misma estructura (`COLUMN_MAP`, mapas de
valores, `validar(df) -> list[str]`, normalización de filas) y su misma regla de oro: **lo que
no está mapeado no se inventa**.

### 2.1 Qué demuestra

1. **Que el *anti-corruption layer* funciona contra un esquema ajeno.** El adaptador ingiere un
   CSV con nombres de columna que no elegimos nosotros, valores en inglés, columnas que sobran
   (demográficas, que ignora y lo dice) y columnas que faltan. El motor, el `FactSet`, el
   verificador numérico y la API **no cambian ni una línea**.
2. **Que el proyecto está listo para el dataset real de Movistar.** Cuando llegue, el trabajo
   es escribir un mapa de columnas en un archivo. Esto ya lo hicimos una vez, contra un esquema
   hostil, y funciona.
3. **Que el control de calidad de la ingesta rechaza y explica.** Sobre el CSV de ejemplo, una
   sola ejecución rechaza cinco filas por cinco motivos distintos, cada uno con su frase.

### 2.2 Qué NO demuestra, y conviene decirlo antes que nadie lo pregunte

- **No demuestra exactitud del motor.** Los recibos que produce son parcialmente sintéticos
  (§3). Medir contra ellos la tasa de alucinación o la precisión de atribución sería medirnos
  contra nuestra propia invención.
- **No aporta realismo a la demo.** La demo en vivo sigue corriendo sobre `C-DEMO-01/02/03` del
  dataset propio, cuyo *ground truth* es exacto.
- **No sustituye al dataset oficial.** Cuando llegue el de Movistar, este adaptador queda como
  lo que es: un ensayo general documentado.

### 2.3 Cómo funciona

```
CSV externo  →  detectar_esquema()  →  validar()  →  sintetizar_cuenta()  →  bills/*.json + ordenes.csv
   ajeno         qué reconozco       qué rechazo    síntesis honesta          forma canónica del proyecto
```

- **`detectar_esquema(df) -> InformeEsquema`** informa de qué columnas reconoció, cuáles faltan
  y cuáles ignora. **No se asume el nombre exacto de las columnas de ningún dataset concreto**:
  `COLUMN_MAP` recoge los nombres más habituales, ya normalizados (minúsculas, sin separadores),
  de modo que `"Monthly Charges"`, `"monthly_charges"` y `"MonthlyCharges"` son la misma clave.
  Añadir un dataset nuevo es añadir alias a ese diccionario, y a nada más.
- **`validar(df) -> list[str]`** aplica los mismos criterios que el hermano y devuelve la lista
  de motivos; lista vacía significa apto.
- **`sintetizar_cuenta(fila)`** reutiliza `packages/datagen` —el mismo motor que genera el
  dataset propio, con la misma conciliación de *ground truth* que aborta si no cuadra— para
  producir el historial canónico.
- La salida se escribe con **la misma forma que el dataset propio**: `bills/{cuenta_id}.json`
  al estilo BrainyBill y `ordenes.csv` con las columnas nativas de Amdocs, escritas por
  `movistar_map`. Se puede servir apuntando `DATOS_SINTETICOS` a ese directorio, sin tocar el
  motor.

### 2.4 Cómo se usa

```bash
# Validar y ver el esquema detectado, sin producir nada
python -m packages.datagen.mapping.kaggle_map \
    --csv data/ejemplos_externos/telco_ficticio.csv --solo-validar

# Ingerir y escribir las cuentas canónicas
python -m packages.datagen.mapping.kaggle_map \
    --csv data/ejemplos_externos/telco_ficticio.csv --salida data/externo/
```

Salida real de la segunda orden (15 filas del CSV de ejemplo, ejecutada el 5 de agosto de 2026):

```
  filas leídas 15 · aceptadas 10 · rechazadas 5
  RECHAZOS (con su motivo):
    RECHAZADO fila 10 (cliente 1010-JJJJJ): antigüedad de 3 meses. Hacen falta al menos 6
      para sintetizar el recibo actual y los cinco previos. Un cliente sin historia
      suficiente no se sintetiza: se descarta.
    fila 11 (cliente 1011-KKKKK): tipo de contrato no mapeado en CONTRATO_MAP: 'Trimestral'.
      Añádalo a este archivo, no al motor.
    fila 12 (cliente 1012-LLLLL): cargo total inválido: importe vacío
    RECHAZADO fila 13 (cliente 1013-MMMMM): el cargo total 21000 céntimos no cuadra con 20
      meses a 7000 céntimos (esperado 140000); desviación de 8500 puntos básicos sobre una
      tolerancia de 2500. Un agregado que no cuadra no se sintetiza: se descarta.
    fila 14 (cliente 1001-AAAAA): identificador duplicado.
  cuentas canónicas producidas: 10 · 60 recibos · 10 órdenes · 20 filas de ground truth
  desviación máxima frente al cargo mensual real: 1 céntimo (S/ 0.01)
```

Y la comprobación que cierra el argumento: sirviendo ese directorio (`DATOS_SINTETICOS=data/externo`),
las **diez** cuentas responden `GET /v1/hechos` con invariante cumplido y `POST /v1/explicar`
con `verificacion_numerica = PASS` y **cero afirmaciones no ancladas**. El motor no se enteró de
que los datos venían de otro sitio, que es exactamente lo que un *anti-corruption layer* debe
conseguir.

### 2.5 El CSV de ejemplo es **inventado por el equipo**

`data/ejemplos_externos/telco_ficticio.csv` — quince filas escritas a mano por nosotros.
**No se descargó de ninguna parte.** No procede de Kaggle ni de ningún otro repositorio, no se
ha muestreado ni derivado de un fichero descargado, y no contiene datos de Movistar ni datos
personales. Reproduce los **nombres de columna habituales** de los datasets tabulares de fuga
para que el adaptador se pueda probar **sin conexión y sin cuenta en ninguna plataforma**. Es,
junto al `README.md` de `data/`, la única excepción a la regla de no versionar nada bajo
`data/`, y la excepción está justificada en el propio `.gitignore`.

**No hemos inspeccionado ningún dataset público concreto para escribir esto**, y por eso este
documento no cita ninguno por su nombre ni afirma nada sobre el contenido de un fichero
específico. Lo que sí se afirma —la forma general de los datasets de fuga— es conocimiento de
dominio, no el resultado de una descarga.

---

## 3. Advertencia metodológica: los recibos derivados son **parcialmente sintéticos**

Esta sección es la razón de ser del documento. **Léase antes de sacar cualquier conclusión de
los datos que produce el adaptador.**

La síntesis separa, campo por campo, tres orígenes, y cada cuenta producida lleva ese desglose
dentro (`procedencia` en su `bills/{cuenta_id}.json`, y `procedencia.json` para el conjunto):

| Etiqueta | Qué incluye |
|---|---|
| `DATASET_EXTERNO` | Identificador de cliente, **antigüedad en meses**, **cargo mensual**, cargo total, tipo de contrato, método de pago y qué servicios tiene contratados |
| `DERIVADO_DEL_DATASET` | Tarifa de cada servicio (el cargo mensual real, desagregado el IGV de ley y repartido entre los servicios que declara el dataset, por mayor resto), modalidad de renta, segmento y cuántos periodos se pueden sintetizar |
| `SINTETIZADO_POR_EL_EQUIPO` | **Todo el desglose por concepto**, las fechas de ciclo, emisión y vencimiento, el IGV línea a línea, el día de ciclo, el nombre del plan, el escenario de variación del último periodo, los movimientos de CRM y el *ground truth* |

**Consecuencia directa, y no es una nota al pie:**

> Los recibos derivados de un dataset externo **sirven para ejercitar la ingesta y NO para
> validar la exactitud del motor**. La evaluación oficial (`make eval`, `TA_respuesta`,
> precisión de atribución) se corre **exclusivamente** sobre el dataset sintético propio, cuyo
> *ground truth* se escribe en el mismo acto de fabricar cada importe y cuya conciliación
> aborta la generación si no cuadra concepto por concepto.

Medir la tasa de alucinación contra estos datos sería una tautología: el sistema no puede
inventar cifras que nosotros mismos hemos inventado antes.

Tres decisiones concretas de honestidad, tomadas dentro del código:

1. **El nombre del plan lleva la marca de origen** (`Plan Externo Hogar`, `Plan Externo Línea
   Fija`…). El nombre del plan viaja dentro del `FactSet` y llega a la explicación del cliente,
   así que la marca de procedencia tenía que ser visible también ahí.
2. **Los beneficios del cliente quedan vacíos.** El dataset no dice nada de ellos y no se
   inventan: el «efecto efervescente» se queda mudo antes que hablar sin dato detrás.
3. **La modalidad de renta va etiquetada como derivada, no como dato.** El dataset externo no
   dice si la renta se cobra adelantada o vencida —esa distinción no existe en un dataset de
   fuga—; la regla `contrato con permanencia → adelantada` es un `[SUPUESTO]` nuestro y está
   marcado como tal en `CONTRATO_MAP`.

### 3.1 Qué se rechaza y por qué

El adaptador aplica los mismos criterios que su hermano, adaptados a este origen:

| Control | Motivo |
|---|---|
| Columnas obligatorias presentes (`cliente_ref`, `antiguedad_meses`, `cargo_mensual`) | Sin cliente o sin importe no hay nada que ingerir |
| Identificador de cliente no vacío y **no duplicado** | Dos filas del mismo cliente producirían dos historiales para la misma cuenta |
| Antigüedad ≥ 6 meses | No se puede sintetizar «el recibo actual y los cinco previos» de un cliente que lleva tres meses |
| Cargo mensual convertible y positivo | Un recibo sin importe no se explica |
| **Cargo total ≈ cargo mensual × antigüedad**, con tolerancia de 2 500 puntos básicos | Equivalente exacto del cuadre de recibo del hermano: un agregado que se contradice a sí mismo no se sintetiza |
| Tipo de contrato y método de pago mapeados | Lo que no está mapeado no se adivina: se reporta y se añade al archivo del ACL, no al motor |

---

## 4. Obligaciones de las BASES sobre datasets de terceros

`[CONFIRMADO-OFICIAL]` BASES, sección 10 «Uso de herramientas de IA generativa»:

> «Se permite el uso de herramientas de desarrollo, plataformas low-code/no-code y herramientas
> de inteligencia artificial generativa. El uso de inteligencia artificial deberá ser declarado,
> especificando las herramientas utilizadas y su rol en la solución. **El uso de datasets, API o
> servicios de terceros deberá ser declarado.**»

`[CONFIRMADO-OFICIAL]` BASES, sección 9:

> «Los participantes garantizan que los contenidos presentados son originales. Todo uso de
> herramientas de terceros (IA generativa, API, open source o datasets) debe cumplir
> estrictamente con sus respectivas licencias, sin vulnerar derechos de propiedad intelectual
> ajenos.»

### 4.1 Qué implica para nosotros hoy

Como **no se usa ningún dataset de terceros**, no hay ninguna licencia de dataset que cumplir ni
ningún tercero al que atribuir. La declaración correspondiente está en
[`docs/declaracion_herramientas.md`](declaracion_herramientas.md), sección 3, y dice exactamente
eso: los dos únicos datasets son el sintético propio y el oficial de Movistar cuando llegue, y el
CSV de ejemplo del adaptador es original del equipo.

### 4.2 Qué habría que hacer si algún día se usara uno `[PROPUESTA]`

Esta es la lista de comprobación que el equipo se impone **antes** de que un solo byte de un
dataset de terceros entre en el proyecto:

1. **Declararlo** en `docs/declaracion_herramientas.md`, sección 3, con: nombre exacto, autor o
   entidad que lo publica, URL, versión o fecha de descarga, **licencia literal** y rol exacto en
   la solución.
2. **Verificar la licencia**, no suponerla. En particular, que permita uso comercial y obras
   derivadas, porque BASES §9 prevé la **cesión de los derechos de propiedad intelectual a
   Integratel**: un dataset con cláusula no comercial o «solo investigación» es incompatible con
   esa cesión y queda descartado de entrada.
3. **Comprobar que no contiene datos personales.** Un dataset de clientes de telecomunicaciones
   con identificadores reales no entra, aunque su licencia lo permita.
4. **No versionarlo.** `data/` está en `.gitignore` con la única excepción documentada del CSV
   de ejemplo propio. Un dataset de terceros vive fuera del control de versiones.
5. **Marcarlo como fuente en la procedencia**, con el mismo mecanismo campo por campo que ya
   usa el adaptador: ningún dato debe poder llegar a la explicación de un cliente sin que se
   sepa de dónde salió.
6. **No usarlo para medir.** Si el dataset requiere síntesis para completar lo que le falta,
   los resultados que produzca no entran en ninguna métrica de exactitud (§3).

---

## 5. Dónde está cada cosa

| Archivo | Qué es |
|---|---|
| `packages/datagen/mapping/movistar_map.py` | ACL del dataset **real** de Movistar. Único archivo que cambia cuando llegue |
| `packages/datagen/mapping/kaggle_map.py` | ACL de **ensayo** contra el esquema tabular de los datasets públicos de fuga |
| `data/ejemplos_externos/telco_ficticio.csv` | CSV de ejemplo **inventado por el equipo**, versionado a propósito |
| `data/ejemplos_externos/README.md` | Qué es y qué no es ese CSV, con la tabla de filas de rechazo |
| `data/externo/` | Salida del adaptador (ignorada por git). `bills/`, `ordenes.csv`, `ground_truth.csv`, `procedencia.json`, `resumen.json` |
| `docs/declaracion_herramientas.md` §3 | Declaración formal de datasets exigida por BASES §10 |
