# El paquete de contexto del asesor

Hackathon AI Telecom Challenge 2026 · Desafío 1 · recibo-claro.

Etiquetado: `[CONFIRMADO-OFICIAL]` cita literal de BASES o de la ficha · `[SUPUESTO]` ·
`[PROPUESTA]` · `[POR VALIDAR]`.

---

## 1. El problema, en una frase

La ficha pide *«flujos de derivación inteligente (hand-off) hacia asesores humanos cuando la
consulta salga del alcance de facturación, **transfiriendo el contexto de la interacción**»*
`[CONFIRMADO-OFICIAL]`. Y el usuario del reto lo dijo más crudo: *«en cualquier caso debemos
notificar al asesor, de alguna forma darle contexto»*.

El fallo que este documento evita no es que el asesor no reciba datos. Es que reciba **los
datos equivocados**: un resumen construido aparte del que se auditó, con cifras que el
verificador nunca aprobó, sin distinguir lo confirmado de lo supuesto. Un asesor que confirma
al cliente una hipótesis del motor convierte un error del sistema en una promesa de la
operadora.

---

## 2. La decisión: un contenido, tres transportes

**Lo común a los tres canales es el contenido. Lo que cambia es el transporte.**

| Canal | Cómo llega el asesor | Qué transporta el paquete |
|---|---|---|
| App Mi Movistar | Entra a la conversación abierta (`POST /v1/asesor/conversacion/{id}/unirse`) | JSON completo en la consola, junto a la transcripción |
| WhatsApp | Toma el número desde su propia herramienta; **no ve nada de nuestro estado** | Texto plano (`GET …/paquete/{ref}/texto`) o tarjeta en la consola |
| Voz (Gemini Live) | Recibe la llamada enrutada | Texto plano en pantalla antes de descolgar + JSON para el detalle |

Un solo contrato: **`PaqueteAsesor`**
(`packages/core_domain/esquemas/paquete_asesor.py`), servido por
**`GET /v1/asesor/paquete/{context_ref}`**.

---

## 3. Qué lleva dentro, y por qué cada campo

| Campo | Por qué está |
|---|---|
| `cuenta_id`, `canal`, `periodo_actual`, `fecha_vencimiento` | La primera frase del asesor: quién es y de qué recibo hablamos |
| `total_previo_cent`, `total_actual_cent`, `delta_total_cent` | El delta exacto. En céntimos enteros, nunca en coma flotante |
| `lineas[]` | **Las líneas que componen el delta**, cada una con su causa, su confianza y si está atribuida. Sin el desglose, el asesor tiene el cuánto y no el porqué |
| `causas[]` | El porqué en vocabulario del cliente, con su peso y su confianza |
| `ya_explicado` | Lo que el cliente **ya oyó**: texto literal, modo, veredicto y **cada cifra entregada** con su estado de anclaje. Es lo que evita que el asesor se repita |
| `incertidumbres[]` | **Qué no se pudo confirmar y por qué.** El campo más importante del paquete (sección 5) |
| `motivo_codigo`, `motivo_detalle`, `accion_pendiente` | Por qué llegó a una persona y qué tiene que hacer esa persona |
| `brief` | La ficha etiquetada (nueve líneas fijas y dos condicionales), para leer en ocho segundos |
| `verificacion_brief` | El veredicto del verificador **sobre el brief**. `PASS` o el brief no se entrega como texto |
| `evidencia` | `trace_id`, `factset_sha256`, hash del último evento, si la cadena valida y la consulta exacta para releerlo todo |

---

## 4. De dónde sale: la bitácora, y solo la bitácora

`packages/governance/paquete_asesor.py::construir_paquete_asesor` lee **exclusivamente**
eventos ya sellados por hash. No toca la memoria del proceso, ni una caché, ni el
*checkpointer*.

```
REQUEST      → cuenta, canal, nivel, conversación, lo que preguntó el cliente
FACTS_BUILT  → totales, delta, líneas con causa y confianza, causas, sha256 del FactSet
INVARIANTE   → si el recibo concilia y con qué residual
ROUTE        → motivo de derivación, señal disparadora, score de incomprensión
VERIFY       → veredicto y cada cifra entregada, con su estado y su fact_id
CITATIONS    → los fact_id citados
RESPONSE     → el texto entregado, el modo, el context_ref
CHAIN        → cuántos eventos tiene el turno y si la cadena está íntegra
```

**Por qué así y no desde el `FactSet` en memoria.** Porque lo que el asesor ve tiene que ser
exactamente lo que se auditó. Si el paquete saliera de un estado paralelo, bastaría un
desajuste —una caché caliente, un reintento, un proceso reiniciado— para que el asesor leyera
algo que ninguna evidencia respalda. Derivándolo de la bitácora, `EvidenciaAuditable` no es
una promesa: es una comprobación que cualquiera puede repetir con
`GET /v1/auditoria?trace_id=…`.

**El paquete describe un caso, no un turno.** Un cliente que pide asesor después de que se le
explicara el recibo genera dos trazas. El constructor recorre toda la conversación hasta el
turno ancla y toma cada dato del evento más reciente que lo declara. Si mirase solo el turno
de derivación diría «aún no se le entregó explicación», que es falso y llevaría al asesor a
repetir lo ya dicho.

### Lo que hubo que añadir a la bitácora

La bitácora guardaba *cuántas* líneas tenía el delta, no *cuáles*. Se ampliaron dos payloads
(cambio aditivo; ningún consumidor existente se rompe):

- `FACTS_BUILT` → `lineas_delta[]`, `causas_detalle[]`, `deuda_anterior_cent`,
  `total_a_pagar_cent`, `dias_ciclo`, `fecha_vencimiento`.
- `RESPONSE` → `texto_entregado` (recortado a `MAX_TEXTO_BITACORA`, 2000 caracteres: la
  bitácora es evidencia, no un almacén de conversaciones).
- `RESPONSE` → `context_ref` **en las tres vías de derivación de `POST /v1/explicar`**, no
  solo en dos. El expediente se guarda con `memoria.guardar_contexto()`, que es RAM, y la
  referencia se resuelve con `traza_de_context_ref()`, que solo lee la bitácora: son dos
  almacenes distintos. La derivación por intención («páseme con un asesor») guardaba en el
  primero y no declaraba en el segundo, así que el cliente recibía su `context_ref` y el
  asesor un `404 CONTEXTO_NO_ENCONTRADO` sobre esa misma referencia. Es la regla general de
  este documento aplicada al propio identificador: **lo que no está en la bitácora, para el
  asesor no existe.**
- `POST /v1/derivacion` emite ahora **también** `FACTS_BUILT`. Antes, un turno de derivación
  registraba su motivo y su `context_ref` pero ni una cifra: el expediente decía a quién
  atender y no qué mirar.

---

## 5. Lo que NO se pudo confirmar

Un traspaso responsable no se mide por los datos que entrega, sino por los que **marca como
inciertos**. `Incertidumbre` tiene código, detalle en una frase, impacto acotado en céntimos
cuando se puede, y las etapas o `fact_id` donde consta.

| Código | Cuándo aparece |
|---|---|
| `INVARIANTE_ROTO` | El detalle no cuadra con la diferencia de totales. **No confirmar importes** |
| `LINEA_SIN_ATRIBUIR` | Hay variación sin causa identificada: el motor ve el cuánto, no el porqué |
| `CAUSA_POCO_FIABLE` | La causa dominante está por debajo de `CONFIANZA_MINIMA_CAUSA` (0.60): es **hipótesis**, no hecho |
| `CIFRA_NO_ANCLADA` | El verificador bloqueó cifras: no se entregaron y tampoco deben entregarse ahora |
| `SIN_EXPLICACION_ENTREGADA` | El asesor **empieza** la conversación, no la retoma |
| `SIN_HECHOS` | No se llegó a abrir el recibo en el caso |
| `CADENA_ROTA` | La cadena de hashes no valida: el paquete no sirve como evidencia |

`CAUSA_POCO_FIABLE` es, hoy, el caso normal en este dataset: sin órdenes de CRM la atribución
causal cae a confianza baja. Decirlo es más honesto que callarlo, y es exactamente la
información que un asesor necesita para no afirmar de más.

---

## 6. El brief pasa por el mismo verificador

El `brief` es texto **generado por el sistema**, así que se le aplica la regla del proyecto:
se redacta solo con cifras del paquete y después se comprueba token a token con el **mismo
extractor** que protege la respuesta al cliente
(`packages.llm_layer.verificador.extraer_aserciones`).

- `tokens_del_paquete()` construye el `ALLOWED` del brief: cifras del recibo, cifras dentro de
  textos ya sellados (nombres comerciales como *«Paquete 5 GB»*) y las tres cifras que el
  brief dice de sí mismo (eventos auditados, cifras ya entregadas, incertidumbres restantes).
- `verificar_brief()` da `PASS` solo si **ninguna** cifra queda sin anclar.
- **Excepción declarada, no silenciosa:** las cifras que escribió el cliente van entre
  comillas en la línea `CONSULTA` y se listan en `citadas_del_cliente`. No son afirmaciones
  del sistema. Censurar la pregunta del cliente sería absurdo; darla por respaldada, deshonesto.

`GET …/paquete/{ref}/texto` **se niega** a devolver texto si el brief no pasó (409
`BRIEF_NO_VERIFICADO`). Un JSON con `veredicto: FAIL` es un dato que el consumidor puede
interpretar; un texto plano con una cifra sin respaldo es una cifra que un asesor va a leer en
voz alta.

Ejemplo real (cuenta de demostración, verificación `PASS`, 0 cifras sin anclar):

```
CLIENTE       C-DEMO-01 · recibo 2026-07 · renta ADELANTADA · vence 13/08/2026
CONSULTA      «quiero hablar con una persona» · canal APP
VARIACIÓN     S/ 195.55 → S/ 216.37 (S/ 20.82)
CAUSA         promociones vencidas · S/ 49.90 (58.47%) · confianza 98 %
YA EXPLICADO  explicación entregada · modo LLM · verificación PASS · 15 cifra(s) ya en manos del cliente
NO CONFIRMADO la causa «cambio de plan» es una hipótesis del motor (confianza 50 %), no un hecho
              confirmado por una orden de servicio: contrastarla antes de afirmarla
DERIVA POR    peticion_explicita:PETICION_HUMANO
PENDIENTE     atender la duda concreta del cliente; la explicación del recibo ya se le dio
EVIDENCIA     bitácora de 16 eventos · cadena íntegra · GET /v1/auditoria?trace_id=tr-65197883f7e5
```

---

## 7. Cómo lo consume cada canal

### 7.1 App Mi Movistar — el asesor entra a la conversación

Ya existe la sala compartida. El paquete se suma como panel lateral.

1. `GET /v1/asesor/cola` → el asesor ve los casos pendientes con su `context_ref`.
2. `GET /v1/asesor/paquete/{context_ref}` → carga el panel: brief arriba, desglose de líneas,
   incertidumbres destacadas, y el texto ya entregado para no repetirlo.
3. `POST /v1/asesor/conversacion/{id}/unirse` → la IA pasa a copiloto.
4. `POST …/mensaje` → escribe. **Su texto no pasa por el verificador**, y es deliberado: el
   verificador avala a la máquina; una persona responde de sus propias palabras.

`[PENDIENTE]` El cliente todavía no puede **leer** al asesor: la única ruta que devuelve
turnos exige `LOA_ASESOR`. Falta un `GET` para el titular. No es parte de este encargo, pero
sin él la sala está escrita por un lado y muda por el otro.

### 7.2 WhatsApp — el asesor toma el número

Tres hechos verificados en la documentación de Meta que **condicionan el diseño**:

1. **El protocolo de traspaso de hilo no existe para WhatsApp** `[CONFIRMADO-OFICIAL]`. Los
   extremos `pass_thread_control` / `take_thread_control` cuelgan de un `PAGE_ID` de Facebook
   y cubren Messenger e Instagram Direct. En WhatsApp **nosotros somos y seguimos siendo el
   único propietario del hilo**: el traspaso es responsabilidad entera de nuestra aplicación.
   No es una carencia del proyecto; es cómo está construida la plataforma.
2. **Meta no guarda la conversación como historial consultable.** Retiene mensajes un máximo
   de 30 días *«in order to provide the base features and functionality of the Cloud API
   service; for example, retransmissions»*, y no hay extremo para releer un hilo. Además, Meta
   actúa como **encargado del tratamiento** y nosotros como responsables. Conclusión: el
   almacén de contexto propio **no es opcional ni es duplicar trabajo**. Es nuestra obligación.
3. **La ventana de servicio de 24 horas gobierna cuándo puede hablar el asesor.** Se abre con
   cada mensaje entrante (o una llamada) y se reinicia con él. Fuera de la ventana solo caben
   plantillas aprobadas de antemano.

Flujo propuesto `[PROPUESTA]`:

1. El bot deriva → se crea el `context_ref` y el expediente entra en la cola.
2. Se **notifica** al asesor con `resumen_para_notificacion()`: quién, por qué, cuántas
   incertidumbres y dónde seguir. **Sin importes**, porque ese aviso viaja por sistemas que no
   son nuestros.
3. El asesor abre la consola y pide `GET …/paquete/{ref}` (o `/texto` si su herramienta solo
   admite texto) y **retoma sin que el cliente repita nada**.
4. La consola muestra el tiempo restante de la ventana de 24 h, calculado sobre la marca del
   último mensaje entrante que guardamos **nosotros**.
5. Si la ventana se cerró, el asesor solo puede reabrir con una plantilla de utilidad. Una
   plantilla candidata: *«Su consulta sobre el recibo de {{periodo}} está siendo revisada por
   un asesor. ¿Desea continuar?»* — sin importes, para que valga como utilidad y no requiera
   nivel de aseguramiento alto. `[POR VALIDAR con Movistar]`

### 7.3 Voz (Gemini Live) — se transcribe, se resume y se enruta

Lo que ya existe: el token efímero de `apps/api/routers/live.py` activa las **dos**
transcripciones (`input_audio_transcription` y `output_audio_transcription`), y la herramienta
`explicar_recibo` llama a `/v1/explicar` conservando el `conversation_id`. La tesis se sostiene
también por voz: el modelo hablado **no calcula**, pide la cifra verificada.

Lo que falta y cómo encaja el paquete `[PROPUESTA]`:

1. Al colgar, el cliente de Live envía la transcripción al backend. Hoy `close()` no manda
   nada y la transcripción se pierde con la pestaña.
2. Esa transcripción se anota como turnos de la conversación; el paquete la recoge por el
   camino normal, sin código especial: el `context_ref` es el mismo.
3. La derivación enruta la llamada y entrega al asesor `a_texto_plano()` **antes de
   descolgar**. Es el caso que más justifica el formato rígido: se lee mientras suena el teléfono.
4. Se añadió `Canal.VOZ` al dominio. Hasta ahora una consulta hablada se auditaba como `APP`
   (`apps/web/src/api/client.ts` fija `canal: "APP"`), con lo que la bitácora no distinguía una
   llamada de una sesión en la App. El cliente de Live debe declarar `canal: "VOZ"`.
5. `[CONFIRMADO-OFICIAL]` Meta **prohíbe la RTC/PSTN en cualquier tramo** de una llamada de
   WhatsApp. Si se promete «le llamamos», el asesor tiene que estar en un extremo VoIP/SIP. La
   cabecera `x-wa-meta-wacid` de la integración SIP es el enganche natural para pegar nuestro
   `context_ref` al tramo de voz.

---

## 8. Seguridad

- El paquete exige nivel **`LOA_ASESOR` literal**, no «que alcance `LOA_ASESOR`».
  `ORDEN_NIVELES` puntúa `LOA2` y `LOA_ASESOR` igual —con razón: un asesor ve lo mismo que el
  titular, con más deberes—, y el efecto colateral era que un token del propio cliente entraba
  en el router del asesor. Para la sala era discutible; para el paquete es inaceptable: lleva
  confianzas del motor, hipótesis e instrucciones internas. Son notas de trabajo, no
  información del cliente sobre sí mismo. Corregido en `apps/api/routers/asesor.py::_solo_asesor`.
- Defensa en profundidad por cuenta: el expediente de otra cuenta responde `403
  CUENTA_NO_AUTORIZADA` aunque el nivel sea correcto.
- Todo acceso al paquete se audita (`ROUTE`, evento `PAQUETE_ENTREGADO`) con el
  `acting_on_behalf_of` del asesor, que `LOA_ASESOR` obliga a declarar.

---

## 9. Qué queda fuera de este encargo

- La integración real con la API de WhatsApp y con Gemini Live: no hay credenciales.
- La cola persistente. `cola`, `prioridad` y `vigencia_min` siguen siendo literales del modelo
  de respuesta de `/v1/derivacion`; el paquete, en cambio, ya es persistente de hecho, porque
  se reconstruye desde el fichero de bitácora y no desde la memoria del proceso.
- La notificación efectiva al asesor. Existe la **carga** (`resumen_para_notificacion()`), no
  el transporte.
- El `GET` de transcripción para el titular en la sala de la App.

---

## 10. Referencias en el código

| Pieza | Fichero |
|---|---|
| Contrato | `packages/core_domain/esquemas/paquete_asesor.py` |
| Constructor y verificación del brief | `packages/governance/paquete_asesor.py` |
| Endpoints | `apps/api/routers/asesor.py` (`/v1/asesor/paquete/{context_ref}`) |
| Detalle que se añadió a la bitácora | `packages/facts_engine/motor.py::detalle_lineas`, `detalle_causas` |
| Pruebas | `tests/unit/test_paquete_asesor.py`, `tests/integracion/test_paquete_asesor_api.py` |
