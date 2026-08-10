# Colección `curl` — recorrido completo de la demo

No hay frontend a propósito: la solución se entrega como API. Esta colección recorre el
flujo entero con los **tres clientes de guion** del dataset sintético e incluye el caso
que dispara la derivación y el caso adversario que demuestra la métrica de alucinación.

Todo lo de aquí funciona **sin base de datos, sin clave de Gemini y sin los mocks
levantados**: con `LLM_MODE=mock` y `BRAINYBILL_BASE_URL` vacía, la API lee el dataset
del disco y responde con la plantilla determinística. Lo mismo con Postgres y Gemini
detrás, cambiando solo variables de entorno.

---

## 0. Preparar el entorno

```bash
# 1. Generar el dataset sintético (una vez; es byte-reproducible con la misma semilla)
python -m packages.datagen.generar --seed 20260804 --clientes 300 --salida data/sintetico

# 2. Arrancar la API
export ENTORNO=dev            # habilita /dev/token y /dev/alucinar
export LLM_MODE=mock          # sin red; con LLM_MODE=gemini y GEMINI_API_KEY usa Gemini
uvicorn apps.api.main:app --port 8000

# 3. (Opcional) Arrancar los mocks de los sistemas de Movistar en otras dos terminales
uvicorn apps.mocks.brainybill.servidor:app --port 8801
uvicorn apps.mocks.amdocs.servidor:app     --port 8802
# y apuntar la API a ellos, sin tocar una línea de código:
export BRAINYBILL_BASE_URL=http://127.0.0.1:8801
export AMDOCS_BASE_URL=http://127.0.0.1:8802
```

```bash
API=http://127.0.0.1:8000
```

> **Windows (PowerShell).** Sustituya `export X=v` por `$env:X = "v"`, `$API` sigue
> funcionando igual y, en lugar de `curl`, use `curl.exe` (el alias `curl` de PowerShell
> es `Invoke-WebRequest` y no entiende `-d`). Los ejemplos con `jq` son opcionales: sin
> `jq`, quite la tubería y lea el JSON entero.

Documentación viva del contrato: <http://127.0.0.1:8000/docs>

---

## 1. Salud — ¿está en pie y contra qué está hablando?

```bash
# Liveness: no toca BrainyBill ni Amdocs (no debe reiniciarse por caída de un tercero)
curl -s $API/salud | jq

# Readiness: corpus RAG, proveedor generativo y cadena de auditoría
curl -s $API/salud/preparacion | jq

# Contra qué sistemas está hablando el ACL ahora mismo (archivo vs. HTTP)
curl -s $API/salud/sistemas | jq
```

`"transporte": "TransporteArchivo"` = leyendo el dataset del disco.
`"transporte": "TransporteHTTP"` = hablando con los mocks (o con el sistema real).

---

## 2. Token — la matriz de niveles de aseguramiento

`POST /dev/token` **solo existe con `ENTORNO=dev`**. En producción el token lo emite el
IdP de Movistar y la API solo lo verifica.

```bash
# LOA2 (App Mi Movistar): explicación completa, con importes
TOKEN=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"C-DEMO-01","nivel":"LOA2","canal":"APP"}' | jq -r .access_token)

# LOA1 (WhatsApp): existencia y dirección del cambio, NINGÚN monto
TOKEN_WA=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"C-DEMO-01","nivel":"LOA1","canal":"WHATSAPP"}' | jq -r .access_token)

# LOA0 (anónimo): solo el catálogo de conceptos
TOKEN_0=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"anonimo","nivel":"LOA0"}' | jq -r .access_token)

# LOA_ASESOR: actúa a nombre de una cuenta; sin `acting_on_behalf_of` NO se emite
TOKEN_ASESOR=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"ASESOR-104-77","nivel":"LOA_ASESOR","acting_on_behalf_of":"C-DEMO-02"}' \
  | jq -r .access_token)
```

```bash
# Prueba de que la obligación del asesor se cumple en el borde: 403 ACTOR_REQUERIDO
curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"ASESOR-104-77","nivel":"LOA_ASESOR"}' | jq
```

> **`account_ref` sale siempre del token.** Ni el cuerpo, ni la query, ni el texto del
> cliente pueden cambiar de cuenta. Enviar `cuenta_id` es opcional y solo sirve como
> confirmación: si no coincide con el token, la respuesta es `403 CUENTA_NO_AUTORIZADA`.

---

## 3. Catálogo — lo único accesible con LOA0

```bash
curl -s $API/v1/catalogo -H "Authorization: Bearer $TOKEN_0" | jq '.[:5]'

curl -s $API/v1/catalogo/PRORRATEO_PLAN -H "Authorization: Bearer $TOKEN_0" | jq
curl -s $API/v1/catalogo/CARGO_RECONEXION -H "Authorization: Bearer $TOKEN_0" | jq
```

`detalle_rag` viene del corpus y pasa por el saneador: si una FAQ decía *"por ejemplo
S/ 49,90"*, aquí aparece `«un monto»`. Un importe de un documento genérico no es un
importe de este cliente y no puede presentarse como si lo fuera; `cifras_retiradas`
enumera lo que se neutralizó.

```bash
# LOA0 no puede pasar de ahí: 403 con el nivel exigido en el cuerpo
curl -s $API/v1/hechos -H "Authorization: Bearer $TOKEN_0" | jq
# -> {"codigo":"NIVEL_INSUFICIENTE","nivel_requerido":"LOA2", ...}
```

---

## 4. Hechos — el `FactSet`, la única fuente de cifras

```bash
curl -s "$API/v1/hechos?periodo=2026-07" -H "Authorization: Bearer $TOKEN" | jq '{
  cuenta_id, periodo_actual, periodo_previo, modalidad_renta,
  total_previo_cent, total_actual_cent, delta_total_cent,
  invariante, confianza_global, sha256,
  causas: [.causas_agregadas[] | {etiqueta_cliente, monto_cent, participacion_bp}]
}'
```

Lo que hay que mirar:

- `invariante.residual_cent` debe ser `0` (tolerancia ±1). Si no lo fuera, este endpoint
  devuelve **`409 INVARIANTE_FALLIDO`** y no hay explicación: se deriva.
- `sha256` sella el documento. Es el mismo valor que aparece en
  `gobernanza.factset_sha256` de la explicación y en el evento `FACTS_BUILT` del log:
  prueba de que el texto se generó sobre estos hechos y no sobre otros.
- Todo importe es un entero en céntimos. `21637` = S/ 216.37.

---

## 5. `C-DEMO-01` — cambio de plan a mitad de ciclo, renta ADELANTADA

**El insight central del proyecto:** el cliente se pasó de un plan de S/ 99.90 a uno de
**S/ 79.90** y su recibo **subió S/ 20.82**. En renta adelantada conviven dos rentas en
el mismo documento (la del mes que empieza, ya con el plan nuevo, y la corrección de los
días del mes que terminó), y además la promoción atada al plan anterior murió con el
cambio. La cuota de equipo financiado, que **no varió**, actúa de distractor: un motor
ingenuo la culparía.

```bash
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
        "conversation_id": "11111111-1111-4111-8111-111111111111",
        "periodo": "2026-07",
        "verbosidad": "CORTO",
        "canal": "APP",
        "utterance": "¿por qué me vino más caro este mes si cambié a un plan más barato?"
      }' | jq
```

Lectura de la respuesta:

```bash
# La prosa que leerá el cliente
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"verbosidad":"DETALLE","utterance":"¿por qué me vino más caro este mes?"}' \
  | jq -r '.bloques[] | select(.tipo=="texto" or .tipo=="aviso") | .texto'

# Los bloques estructurados, con sus importes en céntimos enteros
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"verbosidad":"DETALLE","utterance":"¿por qué me vino más caro este mes?"}' \
  | jq '[.bloques[] | select(.tipo=="kv" or .tipo=="puente" or .tipo=="tabla")]'
```

```bash
# La gobernanza: la parte que le importa al jurado
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"utterance":"¿por qué subió?"}' | jq '.gobernanza | {
        verificacion_numerica, anclado, aserciones_totales,
        aserciones_ancladas, aserciones_no_ancladas, modo, model_version, factset_sha256
      }'
```

`aserciones_no_ancladas` debe ser **0** siempre. Es la métrica comprometida
`TA_respuesta = 0`: cero respuestas con alguna cifra que no esté en el `FactSet`.

Bloques que devuelve, para que cada canal los pinte a su manera:

| `tipo`    | La App lo pinta como            | WhatsApp lo degrada a |
|-----------|---------------------------------|-----------------------|
| `texto`   | párrafo                         | párrafo               |
| `kv`      | tarjeta de importes             | líneas `clave: valor` |
| `puente`  | **gráfico de cascada**          | lista de causas       |
| `tabla`   | tabla de tramos del ciclo       | líneas                |
| `aviso`   | banner                          | párrafo               |

El bloque `puente` se construye desde `causas_agregadas` del `FactSet`: barra de entrada
(recibo previo), una barra por causa oficial y barra de total (recibo actual). Cada barra
es un entero del `FactSet`, así que es anclado por construcción.

```bash
# Guarde el trace para los dos pasos siguientes
TRACE=$(curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"periodo":"2026-07","verbosidad":"DETALLE","utterance":"¿por qué me vino más caro?"}' \
  | jq -r .telemetria.explicacion_id)
echo "trace: $TRACE"   # el mismo valor viaja en la cabecera X-Trace-Id
```

---

## 6. `C-DEMO-02` — corte y reconexión por morosidad, renta VENCIDA

Nueve días suspendido descuentan renta (**−S/ 20.29**) y el cargo de reconexión suma
**S/ 25.00**: el neto es **+S/ 5.56**. Es el caso en que "me cortaron y encima me cobran
más" tiene una explicación exacta, y en que la renta **baja** mientras el recibo **sube**.

```bash
TOKEN2=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"C-DEMO-02","nivel":"LOA2"}' | jq -r .access_token)

curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' \
  -d '{
        "conversation_id": "22222222-2222-4222-8222-222222222222",
        "verbosidad": "DETALLE",
        "utterance": "me cortaron el servicio y aun así me cobran más, ¿por qué?"
      }' | jq '{causas: [.bloques[] | select(.tipo=="puente") | .barras[]],
                verificacion: .gobernanza.verificacion_numerica}'
```

---

## 7. `C-DEMO-03` — fin de descuento prorrateado + deuda arrastrada

Terminó la promoción y, además, arrastra el recibo anterior sin pagar. El total del mes
es **S/ 174.87** pero **el total a pagar es S/ 328.03**: son dos cifras distintas y
confundirlas es una de las causas típicas de reclamo. La deuda anterior **no** entra en
el total del periodo; se muestra aparte, con su propio bloque de aviso.

```bash
TOKEN3=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"C-DEMO-03","nivel":"LOA2"}' | jq -r .access_token)

curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN3" -H 'Content-Type: application/json' \
  -d '{"verbosidad":"DETALLE","utterance":"¿por qué me cobran tanto este mes?"}' \
  | jq '[.bloques[] | select(.tipo=="kv" or .tipo=="aviso")]'
```

---

## 8. Evidencia — de dónde salió cada afirmación

```bash
curl -s $API/v1/evidencia/$TRACE -H "Authorization: Bearer $TOKEN" | jq '{
  total, factset_sha256,
  items: [.items[] | {tipo, ref_id, snippet: (.snippet[0:90])}]
}'

# Solo los tramos: "la tabla de tramos ES la explicación" del prorrateo
curl -s "$API/v1/evidencia/$TRACE?solo=tramo" -H "Authorization: Bearer $TOKEN" | jq
```

Tipos: `factset`, `linea`, `mov` (orden de Amdocs), `tramo`, `cat` (catálogo), `faq` y
`casuistica`. Los tres últimos vienen del corpus y **sin cifras**.

---

## 9. Auditoría — la prueba "comprobable mediante logs de la terminal"

La ficha no pide "cero alucinaciones": pide *"cero invenciones financieras comprobables
mediante logs de la terminal"*. Esto es ese log, y viene encadenado por hash.

```bash
curl -s "$API/v1/auditoria?trace_id=$TRACE&incluir_eventos=false" \
  -H "Authorization: Bearer $TOKEN" | jq -r '.terminal[]'
```

```text
╭─ RECIBO CLARO · trace tr-0b17d48b1ac2 ──────────────────╮
│ AFIRMACIONES NUMÉRICAS 30 · ANCLADAS 30 · NO ANCLADAS 0 │
╰─────────────────────────────────────────────────────────╯
  ✔ PETICIÓN    C-DEMO-01 · 2026-07 · APP · LOA2
  ✔ HECHOS      Δ +S/ 20.82 · 4 líneas · residual 0 c · invariante OK
  ✔ CONTEXTO    5 faq · 1 casuística · saneado · U=0.43
  ✔ GENERACIÓN  mock · mock-plantillas-1.0.0 · 39 ms
  ✔ VERIFICA    PASS · 30 ancladas · 0 derivadas · 0 no ancladas · 30 citas
  ✔ RESPUESTA   7 bloques · 3 acciones · LLM · 39 ms · cadena íntegra (10 eventos)
```

El mismo bloque se imprime en la consola del servidor en cada turno (`LOG_TERMINAL=true`).

```bash
# El detalle completo: una aserción por cifra, con su estado y su fuente en el FactSet
curl -s "$API/v1/auditoria?trace_id=$TRACE&etapas=VERIFY" \
  -H "Authorization: Bearer $TOKEN" | jq '.eventos[0].payload.aserciones[:6]'
```

```json
[
  {"texto": " S/ 20.82", "token_normalizado": "cent:2082",
   "estado": "ANCLADA", "fuente": "factset:delta_total_cent", "derivacion": null},
  {"texto": "31", "token_normalizado": "num:31",
   "estado": "ANCLADA", "fuente": "factset:dias_ciclo", "derivacion": null}
]
```

Cada cifra que se le dijo al cliente, con el campo exacto del `FactSet` que la respalda.
`estado` es `ANCLADA` (está literalmente en el `FactSet`), `DERIVADA` (se obtiene por
álgebra permitida, y entonces `derivacion` dice cuál) o `NO_ANCLADA` — y esta última
nunca llega al cliente.

```bash
# Integridad de toda la bitácora: si alguien retocó un evento, sale el índice roto
curl -s $API/v1/auditoria/cadena -H "Authorization: Bearer $TOKEN" | jq
```

---

## 10. LOA1 — el mismo caso por WhatsApp, sin un solo importe

```bash
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN_WA" -H 'Content-Type: application/json' \
  -d '{"canal":"WHATSAPP","utterance":"¿por qué me vino más caro?"}' \
  | jq '{bloques: [.bloques[] | {tipo, texto}], nivel: .telemetria.redactado_por_nivel}'
```

Compruébelo de verdad: **el texto entregado no contiene ni un dígito**.

```bash
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN_WA" -H 'Content-Type: application/json' \
  -d '{"utterance":"¿por qué subió?"}' | jq -r '.bloques[] | .texto // empty' \
  | grep -c '[0-9]'     # -> 0
```

Se conserva **la dirección del cambio y la causa** ("su recibo subió… el motivo principal
es cambio de plan"), que es exactamente lo que la matriz autoriza en este nivel. Los
bloques `kv`, `puente` y `tabla` desaparecen —son importes por definición— y las citas y
aserciones se vacían, porque `texto_original` de una aserción es literalmente el importe.

```bash
# Y el FactSet completo le sigue estando vedado
curl -s $API/v1/hechos -H "Authorization: Bearer $TOKEN_WA" | jq
# -> 403 NIVEL_INSUFICIENTE, nivel_requerido LOA2
```

---

## 11. Derivación — las dos formas de llegar a un humano

### 11.a Regla dura: el cliente pide una persona

Es el caso del guion. Mismo `conversation_id` en los dos turnos: el segundo dispara la
regla dura `PETICION_HUMANO` y, además, la sonda de silencio del primer turno se cierra
como *repregunta* (no como comprensión).

```bash
CONV=33333333-3333-4333-8333-333333333333

curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CONV\",\"utterance\":\"¿por qué subió mi recibo?\"}" \
  | jq '.gobernanza.verificacion_numerica'

curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CONV\",\"utterance\":\"no entiendo, quiero hablar con una persona\"}" \
  | jq '{derivacion, score: .telemetria.score_incomprension}'
```

```bash
# El brief del asesor, en crudo: siete líneas, menos de ocho segundos de lectura
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CONV\",\"utterance\":\"quiero hablar con un asesor\"}" \
  | jq -r .derivacion.resumen_asesor
```

```text
CLIENTE      C-DEMO-02 · recibo 2026-07 · renta VENCIDA · vence 17/08/2026
CONSULTA     «quiero hablar con un asesor» · canal APP
VARIACIÓN    S/ 92.98 → S/ 98.54 (S/ 5.56)
CAUSA        reconexiones · S/ 25.00 (54.18%) · confianza 0.98 · orden 65431802
YA EXPLICADO explicación entregada · modo LLM · verificación PASS
DERIVA POR   el cliente pidió atención humana ("una persona")
PENDIENTE    atender la duda concreta del cliente; la explicación del recibo ya se le dio
```

### 11.b Hand-off explícito

```bash
CTX=$(curl -s -X POST $API/v1/derivacion \
  -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' \
  -d "{\"conversation_id\":\"$CONV\",\"motivo_codigo\":\"PETICION_HUMANO\",
       \"utterance\":\"quiero que me atienda una persona\"}" | jq -r .context_ref)
echo "context_ref: $CTX"
```

```bash
# Lo que abre el asesor del 104 al recibir la llamada: ficha + FactSet sellado +
# la explicación que ya se le dio al cliente (para no repetírsela)
curl -s $API/v1/derivacion/$CTX -H "Authorization: Bearer $TOKEN2" | jq '{
  cuenta_id, motivo_codigo, factset_sha256, resumen_asesor,
  ya_explicado: .explicacion.texto[0:160]
}'
```

Otros `motivo_codigo` válidos: `INVARIANTE_ROTO`, `CONCEPTO_FUERA_CATALOGO`,
`INTENCION_REGULATORIA`, `UMBRAL_INCOMPRENSION`, `VERIFICACION_FALLIDA`,
`NIVEL_INSUFICIENTE`.

---

## 12. LOA_ASESOR — actuar a nombre de un cliente, y que quede registrado

```bash
curl -s $API/v1/hechos -H "Authorization: Bearer $TOKEN_ASESOR" | jq '.cuenta_id'
# -> "C-DEMO-02": la cuenta sale de `act`, no de `sub` (que es el asesor)
```

```bash
TRACE_A=$(curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN_ASESOR" -H 'Content-Type: application/json' \
  -d '{"canal":"ASESOR","utterance":"el cliente pregunta por el cargo de reconexión"}' \
  | jq -r .telemetria.explicacion_id)

curl -s "$API/v1/auditoria?trace_id=$TRACE_A" -H "Authorization: Bearer $TOKEN_ASESOR" \
  | jq '.eventos[0] | {actor, cuenta_ref, acting_on_behalf_of, nivel}'
```

Cada evento del turno guarda **quién** consultó (`actor`), **a nombre de quién**
(`acting_on_behalf_of`) y **con qué nivel**. Es lo que exige la sección 9 y lo que hace
auditable el acceso de un asesor a los datos de un cliente.

---

## 13. Demo adversaria — que el verificador se vea trabajar

Un `PASS` no prueba nada si nunca se ha visto un `FAIL`. Este endpoint inyecta una cifra
que **no existe en el `FactSet`** dentro de una explicación ya generada.

```bash
# Comparación inmediata: mismo texto, limpio y envenenado
curl -s -X POST $API/dev/alucinar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"activar":true,"cuenta_id":"C-DEMO-01","delta_cent":731}' | jq '.demo | {
        veredicto_limpio, no_ancladas_limpio,
        veredicto_envenenado, no_ancladas_envenenado, infractores, terminal
      }'
```

```text
{
  "veredicto_limpio": "PASS",  "no_ancladas_limpio": 0,
  "veredicto_envenenado": "FAIL", "no_ancladas_envenenado": 1,
  "infractores": ["S/ 28.13"],
  "terminal": [
    "VERIFICACION FAIL  factset=3227801e4fcc",
    "AFIRMACIONES NUMÉRICAS 12 · ANCLADAS 11 · DERIVADAS 0 · NO ANCLADAS 1",
    "  NO ANCLADAS: S/ 28.13"
  ]
}
```

Y ahora, el turno real con el modo activo: **la cifra inventada no llega al cliente**.

```bash
curl -s -X POST $API/v1/explicar \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"utterance":"¿por qué subió?"}' | jq '{
        texto: [.bloques[] | .texto // empty] | join(" "),
        verificacion: .gobernanza.verificacion_numerica,
        derivacion: .derivacion.motivo_codigo,
        cazado: .telemetria.adversaria.infractores
      }'
```

```text
{
  "texto": "Prefiero no darle un número que no pueda sustentar con su recibo. Dejo su consulta con un asesor para que le confirme el detalle exacto.",
  "verificacion": "FAIL",
  "derivacion": "VERIFICACION_FALLIDA",
  "cazado": ["S/ 28.13"]
}
```

El sistema **prefiere callar antes que inventar**, y deriva. El siguiente turno vuelve a
`PASS` solo (el modo consume los turnos que se le pidieron).

```bash
curl -s -X POST $API/dev/alucinar -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"activar":false}' | jq .aviso
```

---

## 14. Errores — los códigos son parte del contrato

```bash
# 401 TOKEN_AUSENTE
curl -s $API/v1/hechos | jq

# 401 TOKEN_INVALIDO
curl -s $API/v1/hechos -H 'Authorization: Bearer no-es-un-token' | jq

# 403 NIVEL_INSUFICIENTE (lleva `nivel_requerido`, para que el canal escale la auth)
curl -s $API/v1/hechos -H "Authorization: Bearer $TOKEN_0" | jq

# 403 CUENTA_NO_AUTORIZADA: el account_ref sale del token, y pedir otra cuenta hace ruido
curl -s "$API/v1/hechos?cuenta_id=C-DEMO-03" -H "Authorization: Bearer $TOKEN" | jq

# 404 CUENTA_NO_ENCONTRADA
TOKEN_X=$(curl -s -X POST $API/dev/token -H 'Content-Type: application/json' \
  -d '{"cuenta_id":"C-NO-EXISTE","nivel":"LOA2"}' | jq -r .access_token)
curl -s $API/v1/hechos -H "Authorization: Bearer $TOKEN_X" | jq

# 404 PERIODO_NO_ENCONTRADO
curl -s "$API/v1/hechos?periodo=2099-01" -H "Authorization: Bearer $TOKEN" | jq

# 404 CONCEPTO_NO_ENCONTRADO (en conversación, este caso deriva por regla dura)
curl -s $API/v1/catalogo/CONCEPTO_INVENTADO -H "Authorization: Bearer $TOKEN_0" | jq

# 422 PETICION_INVALIDA: los modelos son `extra="forbid"`, un campo de más falla
curl -s -X POST $API/v1/explicar -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"utterance":"hola","campo_raro":1}' | jq
```

**`409 INVARIANTE_FALLIDO`** es el código propio de `GET /v1/hechos`: aparece cuando
`|residual_cent| > 1`, es decir, cuando la suma de las variaciones por concepto no
reproduce la diferencia entre totales. No se explica un recibo que no cuadra.

```json
{
  "codigo": "INVARIANTE_FALLIDO",
  "detalle": "la suma de las variaciones por concepto no reproduce la diferencia entre totales; el recibo no se explica, se deriva a un asesor",
  "trace_id": "tr-05859c944d30",
  "datos": {"cuenta_id": "C-DEMO-01", "periodo": "2026-07", "residual_cent": 137, "tolerancia_cent": 1}
}
```

El mismo caso en `POST /v1/explicar` **no es un error**: responde `200` con un bloque de
aviso sin cifras y la derivación ya abierta, con su `context_ref` y su brief.

> **El LLM caído tampoco es un error.** La tabla de la sección 9 anota `424` junto a
> `/v1/explicar`, pero aclara entre paréntesis que degradar a plantilla *"no es error"*.
> Se resuelve como degradación anunciada: `200`, `gobernanza.modo = "PLANTILLA"`,
> cabecera `X-Degradado: PLANTILLA` y `telemetria.degradado = true`. Compruébelo poniendo
> `LLM_TIMEOUT_S=0.001` con `LLM_MODE=gemini` sin clave.

---

## 15. Los mocks, por separado

Son los sistemas de Movistar que la solución consumiría en producción. El ACL
(`apps/api/acl.py`) traduce sus respuestas al modelo canónico; el motor no sabe que
existen.

```bash
# BrainyBill: el recibo actual y los cinco previos
curl -s "http://127.0.0.1:8801/bills/C-DEMO-01?cycles=6" | jq '{cuenta_id, ciclos, periodos}'
curl -s "http://127.0.0.1:8801/bills/C-DEMO-01/2026-07" | jq '.header'
curl -s  http://127.0.0.1:8801/salud | jq

# Amdocs: el historial de órdenes, en su formato nativo…
curl -s "http://127.0.0.1:8802/orders/C-DEMO-01" | jq '.orders'
# …y ya traducido a MovementEvent[] por el mismo mapa que usa el ACL
curl -s "http://127.0.0.1:8802/orders/C-DEMO-01?formato=canonico" | jq '.movimientos'
# control de calidad de ingesta (rechaza tipos de orden no mapeados)
curl -s "http://127.0.0.1:8802/orders/C-DEMO-01/validacion" | jq
```

Fíjese en `ALTA_EQUIPO_FINANCIADO` de `C-DEMO-01`: está fechado en **2024-11**, fuera de
la ventana del ciclo. Es una trampa deliberada — un motor que lo usara para explicar
julio de 2026 estaría atribuyendo mal.

---

## 16. Guion de cinco minutos (demo en vivo)

| # | Comando | Qué se enseña |
|---|---------|---------------|
| 1 | `POST /dev/token` (LOA2) | Autenticación por niveles; nada sensible sin ella |
| 2 | `GET /v1/hechos` de `C-DEMO-01` | Hechos conciliados al céntimo, `residual_cent = 0`, `sha256` |
| 3 | `POST /v1/explicar` de `C-DEMO-01` | **Plan más barato, recibo más caro**: renta adelantada explicada |
| 4 | `GET /v1/auditoria` | `AFIRMACIONES NUMÉRICAS n · ANCLADAS n · NO ANCLADAS 0` |
| 5 | `POST /dev/alucinar` + `POST /v1/explicar` | El verificador caza la cifra falsa y el sistema calla y deriva |
| 6 | `POST /v1/explicar` con `TOKEN_WA` | El mismo caso por WhatsApp, sin un solo dígito |
| 7 | `POST /v1/derivacion` | Hand-off con brief de siete líneas para el asesor del 104 |

Cubre los cinco escenarios críticos que exige la ficha (prorrateos, cuota de equipo
financiado, reconexión tras suspensión, fin de descuentos y cambio de plan) en **ambas
modalidades de renta**: `C-DEMO-01` es ADELANTADA y `C-DEMO-02`/`C-DEMO-03` son VENCIDA.
