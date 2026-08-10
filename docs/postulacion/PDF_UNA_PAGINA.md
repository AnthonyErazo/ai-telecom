# PDF de postulación — una página

> **Formato** `[CONFIRMADO-OFICIAL]` — BASES §4, Fase 3:
> *«Descripción de la solución (PDF de máximo una página, con tipografía Arial tamaño 12 a espaciado simple).»*
>
> Copia el texto de abajo en Word o Google Docs. Arial 12, interlineado sencillo, márgenes de 2 cm.
> **≈ 690 palabras: entra en una página.** Si tu maquetación la desborda, recorta por el orden
> indicado abajo — nunca las cifras.
>
> Entrega el **sábado 15 de agosto**, no el 16. El plazo cierra el domingo a las 23:59 y no
> conviene descubrir un problema de formulario a las 23:00.

---

## TEXTO PARA COPIAR

**RECIBO CLARO — El recibo explicado al céntimo, y con el valor de callar cuando no cuadra**
Desafío 1 · Atención inteligente y explicación de recibos

**El problema.** Movistar emite más de 5 millones de recibos al mes y cerca del 40 % cambia de monto entre meses. Más de 200 mil clientes llaman al 104 para preguntar lo mismo: por qué le vino más caro. La App ya muestra el recibo actual y cinco previos —BrainyBill los expone—, pero no los explica. La demanda de explicación existe, está medida y no está atendida.

**La tesis.** Esa pregunta no es de lenguaje: es de aritmética. Un asistente que se la pase a un modelo generativo devolverá una respuesta plausible y a veces falsa, y en facturación una cifra falsa cuesta un reclamo. Por eso separamos el cálculo de la redacción: **un motor determinístico calcula, un modelo redacta lo ya calculado, y un verificador en código comprueba cifra por cifra que el modelo no inventó nada.**

**Cómo funciona.** El motor parte el ciclo por cada evento —cambio de plan, suspensión, reconexión, fin de promoción, cuota de equipo— y recompone el importe en las dos modalidades de renta, adelantada y vencida. Compara con el mes anterior por concepto y atribuye cada diferencia a su causa desde el historial de órdenes del CRM. Entonces exige un **invariante de conciliación**: la diferencia total debe igualar la suma de las causas, con residual cero. Si no cierra, el sistema **no explica: deriva a un asesor con el caso ya cargado.** Callar es la funcionalidad, no la carencia. Solo con los hechos validados entra el modelo, que recibe un objeto firmado, sin acceso a base de datos y con prohibición de calcular. El RAG aporta lenguaje desde el catálogo de conceptos y las preguntas frecuentes; jamás números.

**Lo que demuestra, medido y reproducible.** Sobre 261 casos: **cero respuestas con una sola cifra sin respaldo** —4.625 afirmaciones numéricas auditadas—, exactitud estricta del 100 %, atribución causal correcta en 391 de 391 conceptos, residual cero. 1.511 pruebas automáticas. La ficha pide que la tasa de alucinación sea *comprobable mediante logs de la terminal*: un botón fuerza al modelo a escribir un monto falso y se ve, en vivo, el veredicto de fallo, el bloqueo y la caída a la explicación determinística. Y el sistema sigue explicando bien **con el modelo apagado**, que es la prueba de que las cifras nunca salieron de él.

**Conversación, no formulario.** Clasifica siete intenciones antes de tocar el recibo, y esa clasificación **es la política**: quien pide la baja o una portabilidad se deriva sin explicación, porque es un trámite contractual; quien intenta manipular al asistente recibe una negativa, su texto **no llega nunca al modelo** y queda registrado. Habla en peruano de chat —entiende «xq me llegó más caro», «no me cuadra»— y sabe que aquí *cancelar* significa pagar, así que «ya cancelé mi recibo» no dispara una baja. Recuerda lo dicho para no repetirse, ajusta la densidad entre respuesta breve y detalle, y cierra recordando los beneficios que el cliente **ya tiene** sin venderlos como nuevos: el efecto efervescente de la ficha. Donde no hay cifras que proteger, el modelo redacta libre; donde las hay, el código las inyecta.

**Un motor, tres canales, tres respuestas.** La misma pregunta se responde distinto según por dónde llegue, sin duplicar lógica. En **Mi Movistar**, con sesión autenticada, la explicación completa con el gráfico de cascada. En **WhatsApp**, donde el número identifica la línea pero no al titular, el asistente dice **si el recibo subió y por qué, y ni un solo importe**, y ofrece seguir en la App: la ficha exige no mostrar información sensible sin autenticación, y el canal transporta el porqué, no el cuánto. Al **asesor del 104** le llega el caso resuelto —resumen, causas y evidencia— para que el cliente no repita nada. Hasta el registro se adapta: *«un poquito más caro»* al cliente, preciso al asesor. Debajo, una capa anticorrupción traduce BrainyBill y Amdocs a un modelo canónico: no reemplazamos ningún sistema. **BrainyBill responde qué se cobró; nosotros, por qué cambió.** Cada respuesta deja una bitácora encadenada por hash que permite reproducir meses después con qué hechos y qué reglas se dijo lo que se dijo. Corre sin internet, sin base de datos y sin claves, en un comando.

---

## Notas de edición

**Qué NO tocar al recortar.** Las cifras oficiales del primer párrafo (5 millones, 40 %, 200 mil) y las medidas del cuarto (261 casos, 4.625 afirmaciones, cero sin anclar). Son lo que separa esta propuesta de las que solo prometen.

**Qué se puede recortar, por orden.**
1. La última frase de «Un motor, tres canales» (*«Corre sin internet…»*).
2. La frase del efecto efervescente.
3. La frase de la bitácora encadenada.

Después de eso ya no queda grasa: cualquier corte adicional quita una prueba.

**Los dos párrafos que no estaban en el primer borrador**, y por qué se añadieron: la ficha pide un asistente *«con tono humano, transparente y horizontal, evitando estructuras robóticas»* y una *«experiencia omnicanal App + Bot (+ WhatsApp)»*, y una propuesta que solo hable del motor determinístico deja sin responder la mitad del enunciado. El verificador es el diferenciador; la conversación y los tres canales son el requisito.

**Sobre la palabra «chatbot».** No aparece como sustantivo del producto. No es por desmarcarse —la ficha pide literalmente *«prototipo funcional de chatbot / asistente conversacional generativo»* y negar el artefacto sería negar el entregable—, sino porque lo que la ficha **califica** es conversacional: tono humano, sin estructuras robóticas, adaptado a la diversidad de clientes. El chat es la interfaz; el motor verificable es lo que se propone.

**Etiquetado de las cifras.** Las del problema son `[CONFIRMADO-OFICIAL]` (ficha del Desafío 1). Las de resultado son medidas propias sobre dataset sintético y **nunca deben presentarse como resultados en clientes reales**. Si el formulario admite nota al pie: *«Métricas medidas sobre dataset sintético propio; validan la mecánica del motor, no predicen desempeño sobre datos reales.»*
