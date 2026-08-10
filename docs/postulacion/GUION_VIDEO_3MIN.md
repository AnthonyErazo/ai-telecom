# Guion del video pitch — 3 minutos

> `[CONFIRMADO-OFICIAL]` BASES §4, Fase 3: *«Video tipo pitch (máximo 3 minutos).»*
>
> **Objetivo del video: pasar el corte de los 20 finalistas.** No es el pitch de la final.
> El comité de la Fase 4 puntúa comprensión del desafío, innovación, viabilidad, impacto y
> calidad del pitch, con 4 puntos cada uno. Este guion está construido para tocar los cinco.

---

## Regla que ordena todo el guion

**El momento memorable ocurre antes del segundo 60.** Un comité que ve veinte vídeos decide en
el primer minuto si sigue mirando. Todo lo demás está subordinado a eso.

Y una decisión de fondo: **el vídeo no explica la arquitectura, la enseña funcionando.**
Un diagrama en un vídeo de tres minutos es tiempo perdido; el diagrama va en las diez
diapositivas de la fase siguiente.

---

## Preparación antes de grabar

| | |
|---|---|
| **Modo** | `LLM_MODE=mock`. Determinista, sin red, sin cuota. Las cifras son idénticas en cada toma |
| **Bitácora** | `make limpiar-datos` antes de la primera toma. Una bitácora heredada rompe la cadena de hashes y el contador saldría en rojo |
| **Secreto JWT** | Cambia `JWT_SECRET` en `.env`: el de ejemplo tiene 23 bytes y suelta un aviso de longitud insegura visible en terminal |
| **Clave de Gemini** | **Cierra el `.env` en el editor.** Está en texto plano. Si se ve en pantalla, se filtra |
| **Pantalla** | 1920×1080, navegador sin pestañas personales, terminal con fuente grande |
| **Cuenta** | `C-DEMO-01` — cambio de plan en renta adelantada con fin de descuento |

---

## Guion segundo a segundo

### 0:00 – 0:14 · El problema, con una cifra y una pregunta

**En pantalla:** un recibo en primer plano, el monto tapado. Sobreimpreso: `+200 000 llamadas/mes`.

> «Doscientas mil llamadas al mes al 104 son la misma pregunta: por qué me vino más caro.
> Movistar ya le muestra al cliente su recibo y los cinco anteriores. Lo que no le muestra es el
> porqué.»

*Por qué así:* la cifra es oficial y verificable, y la pregunta es la que la ficha pone como
canónica. Toca «comprensión del desafío» en catorce segundos.

---

### 0:14 – 0:30 · La tesis, en una frase

**En pantalla:** la consola abierta, panel de chat vacío.

> «Esa pregunta no es de lenguaje: es de aritmética. Si se la pasas a un modelo generativo,
> devuelve una respuesta plausible y a veces falsa. Y en facturación, una cifra falsa cuesta un
> reclamo. Nosotros lo separamos: **el motor calcula, el modelo redacta, y un verificador
> comprueba que el modelo no inventó nada**.»

---

### 0:30 – 0:58 · 🎯 EL PUENTE — el momento

**En pantalla:** se escribe en el chat, en peruano de chat real:

```
xq me llego mas caro?
```

La cascada se dibuja **barra por barra**, con la respuesta apareciendo debajo.

> «Su recibo subió veinte con ochenta y dos. Pero mire la descomposición: **se le venció un
> descuento de cuarenta y nueve noventa**. Y a la vez cambió a un plan más barato, que le
> **ahorró treinta y dos con veintiséis**.»
>
> «Fíjense en lo que acaba de pasar: **el cliente hizo algo que debía bajarle el recibo, y el
> recibo subió**. Un asistente que solo dijera "usted cambió de plan" estaría mintiendo, y
> generaría la segunda llamada.»

*Por qué este caso y no otro:* es contraintuitivo, se entiende sin conocer facturación, y
demuestra atribución causal real en vez de una plantilla. **Este es el segundo 45.**

---

### 0:58 – 1:22 · 🎯 INYECTAR LA ALUCINACIÓN — el diferenciador

**En pantalla:** pantalla partida. Izquierda el chat; derecha la terminal de gobernanza con el
contador grande. Se pulsa el botón rojo **INYECTAR ALUCINACIÓN**.

> «La ficha pide cero alucinaciones financieras **comprobables mediante logs de la terminal**.
> No lo prometemos: se lo enseño.»
>
> *(pulsa)* «Acabo de forzar al modelo a escribir un monto falso.»

La terminal muestra `VERIFY veredicto=FAIL` y la respuesta se bloquea.

> «El verificador lo cazó, bloqueó la respuesta y cayó a la explicación determinística.
> **El cliente no vio ni un dígito inventado.** Sobre 261 casos de evaluación: cuatro mil
> seiscientas veinticinco afirmaciones numéricas auditadas, **cero sin respaldo**.»

*Por qué es el diferenciador:* casi todos van a **afirmar** que no alucinan. Este es el único
que lo **falsea en vivo**. Es «innovación» y «viabilidad» a la vez.

---

### 1:22 – 1:40 · El sistema que sabe callar

**En pantalla:** un caso con el invariante roto. El sistema no explica.

> «Y cuando las cuentas no cuadran, no aproxima. Se calla y deriva a un asesor con el caso ya
> cargado. **Saber decir "no lo sé" es un requisito, no una carencia.** El asesor recibe el
> contexto completo: el cliente no repite nada.»

---

### 1:40 – 1:58 · Conversación, no formulario

**En pantalla:** tres mensajes encadenados, rápidos.

> «Y no responde lo mismo a todo. Clasifica siete intenciones **antes** de tocar el recibo.»
>
> *(escribe «quiero darme de baja»)* «Si el cliente pide la baja, **no le explicamos el recibo:
> lo derivamos**, porque es un trámite contractual, no una duda de facturación.»
>
> *(escribe una inyección de prompt)* «Si alguien intenta manipular al asistente, recibe una
> negativa, **su texto no llega nunca al modelo** y queda registrado como incidente.»
>
> «Habla como se escribe aquí: entiende «xq me llegó más caro» y sabe que en Perú **cancelar
> significa pagar**, así que "ya cancelé mi recibo" no dispara una baja.»

*Por qué está aquí:* la ficha pide *tono humano, transparente y horizontal, evitando estructuras
robóticas*. Esto lo demuestra en dieciocho segundos en vez de afirmarlo.

---

### 1:58 – 2:26 · 🎯 UN MOTOR, TRES CANALES, TRES RESPUESTAS

**En pantalla:** el selector de canal. **La misma pregunta**, tres veces, en tres columnas.

> «La ficha pide experiencia omnicanal. No son tres interfaces: es un motor que responde
> distinto según por dónde le hablen.»

*(canal App)* → aparece la explicación completa con el puente.

> «En Mi Movistar, con sesión iniciada: todo el detalle.»

*(canal WhatsApp)* → la respuesta sale **sin un solo número**.

> «En WhatsApp, el número identifica la línea pero **no al titular**. Así que el asistente le
> dice si su recibo subió y por qué… **y ni un solo importe.** Le ofrece continuar en la App.
> La ficha lo exige: nada sensible sin autenticación. El canal transporta el porqué, no el
> cuánto.»

*(canal Asesor)* → el brief del 104.

> «Y al asesor le llega el caso ya resuelto: resumen, causas y evidencia. **El cliente no repite
> nada.**»

*Por qué es un momento y no una frase:* es la prueba visual de que la omnicanalidad es real y no
tres pantallas desconectadas. Y el contraste del cero en WhatsApp se entiende sin explicación.

---

### 2:26 – 2:38 · Integración: no reemplazamos nada

**En pantalla:** la terminal de auditoría con la cadena de hashes.

> «Nada de esto reemplaza un sistema de Movistar. **BrainyBill responde qué se cobró; nosotros,
> por qué cambió.** Somos el skill de facturación que a Lucía hoy le falta. Y cada respuesta deja
> una bitácora encadenada: dentro de seis meses se puede reproducir con qué hechos y qué reglas
> se dijo exactamente esto.»

---

### 2:38 – 2:52 · Honestidad, que también puntúa

**En pantalla:** la salida de `make eval` con la advertencia de circularidad visible.

> «Una advertencia que publicamos nosotros mismos: estas métricas se miden sobre datos
> sintéticos propios. Validan que el motor es correcto; **no predicen el desempeño con datos
> reales de Movistar**. La única garantía que se traslada tal cual es el cero: el verificador no
> compara contra una verdad de referencia, compara contra el recibo del propio cliente.»

*Por qué incluirlo:* un comité técnico premia a quien conoce los límites de su medición. Y
desactiva la pregunta incómoda antes de que la hagan.

---

### 2:52 – 3:00 · Cierre

**En pantalla:** el nombre y el equipo.

> «Recibo Claro. Que el cliente entienda su recibo sin llamar a nadie — y que cuando no podamos
> explicárselo, lo digamos.»

---

## Plan de contingencia

| Nivel | Qué falla | Qué se hace |
|---|---|---|
| **N1** | Nada | Grabación en vivo con `LLM_MODE=mock` |
| **N2** | La consola no carga | Vídeo de respaldo de 40 s del Puente y de la alucinación, grabado el día anterior |
| **N3** | Falla todo | Capturas de pantalla en secuencia, con la misma locución |

**Graba los tres momentos por separado y en varias tomas.** El Puente, la alucinación y el
invariante roto son bloques independientes: si uno sale mal, se repite solo ese.

---

## Comprobación antes de subir

- [ ] Dura **menos de 3:00**. Cronometrado, no estimado
- [ ] El momento memorable ocurre **antes del segundo 60**
- [ ] No se ve el `.env`, ni la clave de Gemini, ni pestañas personales
- [ ] El contador de la terminal se lee a pantalla completa
- [ ] Las cifras dichas coinciden con las de la pantalla, **al céntimo**
- [ ] Se dice «recibo», nunca «factura»
- [ ] **Se ven los tres canales** y en WhatsApp **no aparece ningún importe**
- [ ] Se demuestra al menos una intención que **no** es explicar el recibo
- [ ] Aparece la advertencia de circularidad
- [ ] Los cuatro integrantes salen o se nombran, con sus carreras (`[CONFIRMADO-OFICIAL]` BASES §5: equipo mixto y mínimo dos carreras)

---

## Reparto sugerido de voces

Que hablen **dos** personas, no una: el comité valora un equipo, no un solista. Una voz para el
problema y el impacto (perfil de negocio), otra para la demostración técnica. El cambio de voz
en el segundo 30 además rompe la monotonía justo cuando aparece el Puente.
