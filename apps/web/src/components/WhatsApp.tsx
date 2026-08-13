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

/** El mensaje tal y como se lee en un móvil: la explicación, y nada más.
 *
 * Se quedan **solo los bloques narrativos**. El desglose en clave-valor y el gráfico de
 * cascada son piezas de pantalla: convertidos a lista de viñetas triplicaban el largo del
 * mensaje repitiendo cifras que la narración ya cuenta, y por WhatsApp eso no es
 * información, es un muro que nadie lee. Tampoco se ponen títulos: en un chat, un
 * encabezado en negrita a media conversación no orienta, interrumpe.
 *
 * No es una versión recortada de la verdad: la explicación completa está ahí. Lo que se
 * quita es el andamiaje visual que este transporte no sabe pintar.
 */
function aTextoPlano(bloques: Block[]): string {
  return bloques
    .filter((bloque) => bloque.tipo === "texto" || bloque.tipo === "aviso")
    .map((bloque) => (bloque as { texto: string }).texto.trim())
    .filter(Boolean)
    .join("\n\n");
}

type Mensaje = { rol: "cliente" | "bot"; texto: string; explicacion?: Explanation };

export function WhatsApp({ cuentaSugerida }: { cuentaSugerida: string }) {
  // WhatsApp **no tiene sesión**, y esa es justamente la premisa del canal: a un cliente
  // que escribe por WhatsApp lo identifica su número de teléfono, no un login. Exigirle
  // entrar antes contradecía el motivo por el que este canal es LOA1. La cuenta se
  // resuelve sola —la que la API declara servible— igual que una pasarela real resolvería
  // el número entrante contra la planta de clientes.
  const [cuenta, setCuenta] = useState(cuentaSugerida);
  const [token, setToken] = useState("");
  const [mensajes, setMensajes] = useState<Mensaje[]>([]);
  const [borrador, setBorrador] = useState("¿por qué me llegó distinto el recibo?");
  const [error, setError] = useState("");
  const [conversacion, setConversacion] = useState<string | undefined>();
  const fin = useRef<HTMLDivElement | null>(null);

  // Si no llega cuenta sugerida —se entró directo a WhatsApp sin pasar por Mi Movistar—
  // se pregunta a la API cuál puede atender. El usuario no tiene que hacer nada.
  useEffect(() => {
    if (cuenta) return;
    let vivo = true;
    api.accounts()
      .then((datos) => { if (vivo && datos.demo?.length) setCuenta(datos.demo[0]); })
      .catch((causa) => { if (vivo) setError(mensajeDeError(causa)); });
    return () => { vivo = false; };
  }, [cuenta]);

  useEffect(() => {
    if (!cuenta) return;
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


  return <div className="wa">
    <div className="wa-phone">
      <header className="wa-bar">
        <span className="wa-avatar">M</span>
        <div>
          <strong>Movistar Perú</strong>
          <small>{token ? "en línea" : "conectando…"}</small>
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

    {/* El panel explicativo se retiró: este canal ocupa la pantalla entera y es la
        vista del cliente. Lo que había que demostrar —que aquí no viaja ni un
        importe— se demuestra mejor en el propio mensaje que en una nota al lado. */}
  </div>;
}

function mensajeDeError(causa: unknown) {
  return causa instanceof ApiError ? `${causa.code}: ${causa.message}`
    : causa instanceof Error ? causa.message : "Ocurrió un error inesperado";
}
