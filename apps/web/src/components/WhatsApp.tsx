import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, ApiError } from "../api/client";
import type { Block, Explanation } from "../api/types";

/**
 * Simulador del canal WhatsApp.
 *
 * Qué demuestra, y por qué es una pantalla y no una nota en el README
 * ------------------------------------------------------------------
 * Dos cosas que en la App no se ven:
 *
 * 1. **Un contenido, tres transportes.** Se llama al MISMO `POST /v1/explicar` y se
 *    recibe la MISMA `RespuestaCanalAgnostica`. Lo único que cambia es que aquí los
 *    bloques se aplanan a texto, porque WhatsApp no sabe pintar una tabla ni un gráfico
 *    de cascada. El servidor no redacta una versión especial para este canal.
 *
 * 2. **La identidad manda sobre lo que se entrega.** En WhatsApp la identidad se apoya en
 *    un número de teléfono, así que el token es `LOA1`, y con `LOA1` el servidor entrega
 *    la explicación **sin un solo importe** (`redactar_para_nivel`, §6.4 del README). Eso
 *    no lo decide esta pantalla: si aquí se pidiera un token `LOA2`, saldrían las cifras.
 *    Se muestra el contador de dígitos precisamente para que se pueda comprobar en vivo.
 */

/** Aplana los bloques a texto corrido, que es lo único que viaja por WhatsApp. */
function aTextoPlano(bloques: Block[]): string {
  const partes: string[] = [];
  for (const bloque of bloques) {
    if (bloque.tipo === "texto" || bloque.tipo === "aviso") {
      if (bloque.titulo) partes.push(`*${bloque.titulo}*`);
      partes.push(bloque.texto);
    } else if (bloque.tipo === "kv") {
      if (bloque.titulo) partes.push(`*${bloque.titulo}*`);
      partes.push(bloque.items.map((i) => `• ${i.clave}: ${i.valor}`).join("\n"));
    } else if (bloque.tipo === "tabla") {
      if (bloque.titulo) partes.push(`*${bloque.titulo}*`);
      partes.push(bloque.filas.map((f) => `• ${f.join(" · ")}`).join("\n"));
    } else {
      // El puente es un gráfico: por WhatsApp se cuenta como lista, en el mismo orden.
      if (bloque.titulo) partes.push(`*${bloque.titulo}*`);
      partes.push(bloque.barras.map((b) => `• ${b.etiqueta}`).join("\n"));
    }
  }
  return partes.filter(Boolean).join("\n\n");
}

type Mensaje = { rol: "cliente" | "bot"; texto: string; explicacion?: Explanation };

export function WhatsApp({ cuenta }: { cuenta: string }) {
  const [token, setToken] = useState("");
  const [mensajes, setMensajes] = useState<Mensaje[]>([]);
  const [borrador, setBorrador] = useState("¿por qué me llegó distinto el recibo?");
  const [error, setError] = useState("");
  const [conversacion, setConversacion] = useState<string | undefined>();
  const fin = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let vivo = true;
    api.tokenWhatsapp(cuenta)
      .then((t) => { if (vivo) { setToken(t.access_token); setError(""); } })
      .catch((causa) => { if (vivo) setError(mensajeDeError(causa)); });
    return () => { vivo = false; };
  }, [cuenta]);

  const enviar = useMutation({
    mutationFn: (texto: string) =>
      api.explicarWhatsapp(token, { conversation_id: conversacion, cuenta_id: cuenta, utterance: texto }),
    onSuccess: (respuesta) => {
      setConversacion(respuesta.conversation_id);
      setMensajes((previos) => [
        ...previos,
        { rol: "bot", texto: aTextoPlano(respuesta.bloques), explicacion: respuesta },
      ]);
    },
    onError: (causa) => setError(mensajeDeError(causa)),
  });

  useEffect(() => {
    fin.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [mensajes, enviar.isPending]);

  const submit = (evento: FormEvent) => {
    evento.preventDefault();
    const texto = borrador.trim();
    if (!token || !texto || enviar.isPending) return;
    setMensajes((previos) => [...previos, { rol: "cliente", texto }]);
    setBorrador("");
    setError("");
    enviar.mutate(texto);
  };

  const ultima = [...mensajes].reverse().find((m) => m.explicacion)?.explicacion;
  // La comprobación que hace honesta a esta pantalla: si el servidor hubiera dejado
  // escapar un importe, aquí se vería. Se cuentan dígitos sobre el texto ENTREGADO.
  const digitos = mensajes.filter((m) => m.rol === "bot").join("").replace(/\D/g, "").length;

  return <div className="wa">
    <div className="wa-phone">
      <header className="wa-bar">
        <span className="wa-avatar">M</span>
        <div>
          <strong>Movistar Perú</strong>
          <small>{token ? "en línea · cuenta " + cuenta : "conectando…"}</small>
        </div>
      </header>

      <div className="wa-chat">
        <div className="wa-aviso">
          Identidad verificada por número de teléfono (<b>LOA1</b>). Por este canal no se
          envían importes.
        </div>
        {mensajes.map((m, i) => <div className={`wa-msg ${m.rol}`} key={i}>{m.texto}</div>)}
        {enviar.isPending && <div className="wa-msg bot wa-esperando">escribiendo…</div>}
        {error && <div className="wa-error">{error}</div>}
        <div ref={fin} />
      </div>

      <form className="wa-form" onSubmit={submit}>
        <input aria-label="Mensaje de WhatsApp" value={borrador} maxLength={2000}
               placeholder="Escriba un mensaje" onChange={(e) => setBorrador(e.target.value)} />
        <button disabled={!token || enviar.isPending} aria-label="Enviar">➤</button>
      </form>
    </div>

    <aside className="wa-nota panel">
      <p className="eyebrow">Por qué esto no es otra API</p>
      <h2>El mismo contenido, otro transporte</h2>
      <p>
        Esta pantalla llama al mismo <code>POST /v1/explicar</code> y recibe la misma
        respuesta que la App. Lo único que cambia es el <b>nivel del token</b>: aquí es
        <b> LOA1</b>, y el servidor entrega la explicación sin importes. La pantalla no
        borra nada.
      </p>
      <div className="wa-metricas">
        <div><strong>{digitos}</strong><small>dígitos entregados</small></div>
        <div><strong>{ultima?.gobernanza.verificacion_numerica ?? "—"}</strong><small>verificación</small></div>
        <div><strong>{ultima?.gobernanza.aserciones_totales ?? "—"}</strong><small>afirmaciones</small></div>
      </div>
      {ultima && <p className="wa-pie">
        {digitos === 0
          ? "Cero dígitos en el texto entregado: es la redacción por nivel, comprobada sobre lo que se envió."
          : "Se detectaron dígitos en el texto: revise redactar_para_nivel."}
      </p>}
      {ultima?.derivacion.requerida && <div className="handoff">
        <strong>Derivación abierta</strong>
        <p>{ultima.derivacion.motivo}</p>
      </div>}
    </aside>
  </div>;
}

function mensajeDeError(causa: unknown) {
  return causa instanceof ApiError ? `${causa.code}: ${causa.message}`
    : causa instanceof Error ? causa.message : "Ocurrió un error inesperado";
}
