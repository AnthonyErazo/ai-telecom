import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
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
    // Un espacio, no un salto de párrafo: en WhatsApp cada salto es una burbuja más alta,
    // y desde que cada causa viaja en su propio bloque —para poder señalarla en el recibo
    // guiado— el mismo texto ocupaba el triple. Se lee de un vistazo, que es lo que pide
    // el canal.
    .join(" ");
}

/** Enlaces clicables, como en un WhatsApp de verdad.
 *
 * El servidor manda la ficha de Google Play de la App Mi Movistar en texto plano —es un
 * canal de texto, no puede mandar otra cosa— y el cliente de WhatsApp la convierte en un
 * enlace tocable. Aquí se hace lo mismo, porque si no, el gesto que la respuesta ofrece
 * («ingrese a la App, aquí la encuentra») en la demo no se puede completar: quedaría una
 * URL pintada que nadie puede tocar. No hay `dangerouslySetInnerHTML`: se parte la cadena
 * y React pinta los trozos, así que el texto del servidor nunca se interpreta como HTML.
 */
// Dos patrones y no uno: `split` necesita el grupo de captura, y el `test` de cada trozo
// tiene que hacerse SIN la bandera `g` —un regex global guarda `lastIndex` entre llamadas
// y `test` devolvería `true`/`false` alternándose sobre el mismo texto.
const _PARTIR_URL = /(https?:\/\/\S+)/g;
const _ES_URL = /^https?:\/\/\S+$/;

function conEnlaces(texto: string) {
  return texto.split(_PARTIR_URL).map((trozo, i) =>
    _ES_URL.test(trozo) ? (
      <a key={i} href={trozo} target="_blank" rel="noopener noreferrer">{trozo}</a>
    ) : (
      trozo
    ),
  );
}

type Mensaje = { rol: "cliente" | "bot" | "asesor"; texto: string; explicacion?: Explanation };

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
  const mensajesAsesorVistos = useRef(new Set<string>());

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

  // Cuando un asesor toma el caso, sus turnos llegan al MISMO chat. Solo se incorporan
  // los de rol `asesor`: los demás ya los pintó este cliente al enviarlos o al recibir
  // la respuesta de BillSense.
  const salaCliente = useQuery({
    queryKey: ["sala-cliente", token, conversacion],
    queryFn: () => api.estadoCliente(token, conversacion!),
    enabled: Boolean(token && conversacion),
    refetchInterval: 3000,
  });
  useEffect(() => {
    const nuevos = salaCliente.data?.turnos.filter((turno) => {
      const clave = `${turno.ts ?? ""}:${turno.texto}`;
      if (turno.rol !== "asesor" || mensajesAsesorVistos.current.has(clave)) return false;
      mensajesAsesorVistos.current.add(clave);
      return true;
    }) ?? [];
    if (nuevos.length) {
      setMensajes((previos) => [
        ...previos,
        ...nuevos.map((turno) => ({ rol: "asesor" as const, texto: turno.texto })),
      ]);
    }
  }, [salaCliente.data]);

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
        {mensajes.map((m, i) => (
          <div className={`wa-msg ${m.rol === "asesor" ? "bot asesor" : m.rol}`} key={i}>
            {m.rol === "asesor" && <small>Asesor Movistar</small>}
            {conEnlaces(m.texto)}
          </div>
        ))}
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
