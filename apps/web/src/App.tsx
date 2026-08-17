import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./api/client";
import type { Explanation, FactSet } from "./api/types";
import { Blocks, money } from "./components/Blocks";
import { WhatsApp } from "./components/WhatsApp";
import { Asesor } from "./components/Asesor";
import { MiMovistar } from "./components/MiMovistar";
import { ReciboGuiado } from "./components/ReciboGuiado";

// Solo se usan si `/dev/cuentas` no contesta. Son las cuentas del dataset sintético, así
// que dependen de que la API esté sirviendo el disco: cuando sirve Supabase, las cuentas
// buenas llegan de la API y estas no valen. Por eso NO se precarga ninguna en el campo
// —hacerlo dejaba «C-DEMO-01» escrito de entrada y, contra el dataset real, entrar sin
// tocar nada fallaba con «la cuenta no existe»—.
const fallback = ["C-DEMO-01", "C-DEMO-02", "C-DEMO-03"];
type View = "whatsapp" | "mimovistar" | "guiado" | "conversacional" | "asesor";

// La app se sirve bajo `base: "/ui/"` (vite.config.ts): una ruta absoluta como
// "/billsense-logo.png" apunta a la raíz del dominio, no a donde vite publica
// `public/`, y el navegador la resuelve como imagen rota. `BASE_URL` la coloca
// donde de verdad vive el archivo, en dev y en build.
const billsenseLogo = `${import.meta.env.BASE_URL}billsense-logo.png`;

/** Un turno de la conversación, tal y como se pinta.
 *
 * La respuesta se guarda **entera** y no solo su texto: los bloques llevan el desglose
 * de líneas y el gráfico de cascada, que es justo lo que distingue a este producto de un
 * chat cualquiera. Guardar el turno del cliente aparte permite además pintarlo antes de
 * que el servidor conteste. */
type Turno =
  | { rol: "cliente"; texto: string }
  | { rol: "asistente"; explicacion: Explanation };

export default function App() {
  const [view, setView] = useState<View>("whatsapp");
  const [account, setAccount] = useState("");
  const [accountDraft, setAccountDraft] = useState("");
  const [token, setToken] = useState("");
  const [facts, setFacts] = useState<FactSet | null>(null);
  const [question, setQuestion] = useState("");
  const [detail, setDetail] = useState("CORTO");
  const [explanation, setExplanation] = useState<Explanation | null>(null);
  const [turns, setTurns] = useState<Turno[]>([]);
  const [audit, setAudit] = useState<Record<string, unknown> | null>(null);
  // Cambia SOLO al cerrar sesión, y se usa como `key` de Mi Movistar para remontarla.
  // Atarla a la cuenta no servía: entrar también cambia la cuenta, y el remontaje
  // borraba la pantalla del recibo justo después de conseguirla.
  const [sesionesCerradas, setSesionesCerradas] = useState(0);
  const [error, setError] = useState("");
  const finDeLaConversacion = useRef<HTMLDivElement | null>(null);

  const health = useQuery({ queryKey: ["health"], queryFn: api.health, retry: 1 });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const accountList = accounts.data?.demo?.length ? accounts.data.demo : fallback;

  // El campo se rellena con la primera cuenta que la API declara servible, y solo
  // mientras el usuario no haya escrito: así entrar de un clic funciona, y funciona con
  // el origen que esté configurado, no con el que estaba el día que se escribió esto.
  //
  // Se lee de `accounts.data`, NO de `accountList`: esa cae en `fallback` mientras la
  // consulta está en vuelo, así que rellenar desde ella escribía «C-DEMO-01» en el primer
  // render y —como esto solo actúa con el campo vacío— ahí se quedaba cuando llegaban las
  // cuentas de verdad. Entrar sin tocar nada fallaba igual que antes, con el agravante de
  // que el desplegable ya mostraba las buenas.
  useEffect(() => {
    const primera = accounts.data?.demo?.[0];
    if (primera && !accountDraft) setAccountDraft(primera);
  }, [accounts.data, accountDraft]);

  // La cuenta con la que se va a entrar: lo escrito, o la primera que se sepa servible.
  // **Un solo valor** para el botón y para el manejador: si se calculan por separado se
  // desincronizan, y así fue como el botón quedó habilitado prometiendo una acción que
  // el manejador descartaba —o, en su versión anterior, deshabilitado sin explicar por
  // qué mientras el desplegable ya mostraba cuentas válidas—.
  const cuentaElegida = (accountDraft || accountList[0] || "").trim();

  const bootstrap = useMutation({
    mutationFn: async (id: string) => {
      const issued = await api.token(id);
      const loadedFacts = await api.facts(issued.access_token);
      return { account: id, token: issued.access_token, facts: loadedFacts };
    },
    onSuccess: (data) => {
      setAccount(data.account);
      setAccountDraft(data.account);
      setToken(data.token);
      setFacts(data.facts);
      setExplanation(null);
      setTurns([]);
      setAudit(null);
      setError("");
    },
    onError: (cause) => setError(message(cause)),
  });

  const explain = useMutation({
    mutationFn: (utterance: string) => api.explain(token, {
      conversation_id: explanation?.conversation_id,
      cuenta_id: account,
      verbosidad: detail,
      utterance,
    }),
    onSuccess: async (result) => {
      setExplanation(result);
      setTurns((previous) => [...previous, { rol: "asistente", explicacion: result }]);
      setError("");
      try { setAudit(await api.audit(token, result.trace_id)); } catch { setAudit(null); }
    },
    // El turno del cliente ya está pintado cuando esto ocurre: se deja y se marca el
    // fallo aparte. Borrarlo dejaría la pantalla como si nunca hubiera preguntado.
    onError: (cause) => setError(message(cause)),
  });

  const adversarial = useMutation({
    mutationFn: () => api.hallucinate(token, account),
    onSuccess: (result) => setAudit(result),
    onError: (cause) => setError(message(cause)),
  });

  // El último turno a la vista. En un chat, lo que se acaba de decir tiene que quedar
  // delante sin que nadie desplace la página a mano. Va después de `explain` porque
  // depende de su estado: declararlo antes lo dejaría en la zona muerta temporal.
  useEffect(() => {
    // Llamada **opcional**: `scrollIntoView` no existe en jsdom, y sin el `?.` un detalle
    // cosmético tumbaba el componente entero durante las pruebas. Desplazar la vista no
    // puede ser nunca motivo de que la aplicación deje de pintarse.
    finDeLaConversacion.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
  }, [turns, explain.isPending]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const texto = question.trim();
    if (!token || !texto || explain.isPending) return;
    // El turno del cliente se pinta **antes** de que responda el servidor. Una
    // explicación tarda unos segundos, y durante esos segundos la pantalla tiene que
    // demostrar que el mensaje se envió; si no, se vuelve a pulsar.
    setTurns((previous) => [...previous, { rol: "cliente", texto }]);
    setQuestion("");
    explain.mutate(texto);
  };

  const login = (event: FormEvent) => {
    event.preventDefault();
    if (cuentaElegida) bootstrap.mutate(cuentaElegida);
  };

  const selectView = (next: View) => setView(next);

  const logout = () => {
    setSesionesCerradas((n) => n + 1);
    setToken("");
    setAccount("");
    setFacts(null);
    setExplanation(null);
    setTurns([]);
    setAudit(null);
    setError("");
    setView("mimovistar");
  };

  const governance = explanation?.gobernanza;
  // `GET /v1/auditoria` ya publica el mismo resumen de seis líneas que el proyecto
  // imprime en la terminal: veredicto, afirmaciones ancladas y una línea por etapa. Es
  // lo que un humano puede leer de un vistazo, así que es lo que se enseña; el volcado
  // completo queda debajo, para quien vaya a auditar de verdad.
  const auditLines = Array.isArray(audit?.terminal) ? (audit.terminal as string[]) : [];
  return <div className="shell">
    <header className="topbar">
      <a className="brand" href="/ui/"><img src="https://cert-cdn.movistar.com.pe/2024/12/logo-2.svg" alt="Movistar" className="brand-logo" /><div><strong>MOVISTAR</strong></div></a>
      {/* Un canal, una pestaña. Ni más. El login dejó de ser un destino: es un paso DENTRO
          de Mi Movistar, que es donde el cliente se autentica de verdad. WhatsApp y la
          consola del 104 no lo necesitan —uno identifica por teléfono y el otro por
          credencial de asesor—, así que ninguna está bloqueada por una sesión ajena. */}
      {/* Tres canales, tres pestañas, y cada uno ocupa la pantalla entera. Antes se
          compartía el ancho con el panel de gobernanza y ninguna de las dos cosas se
          leía bien: una maqueta de móvil encajonada en media pantalla no parece un
          móvil, y un panel de auditoría en una columna estrecha no se audita. */}
      <nav className="app-tabs" aria-label="Navegación principal">
        <button type="button" className={view === "whatsapp" ? "active" : ""} aria-pressed={view === "whatsapp"} onClick={() => void selectView("whatsapp")}>WhatsApp</button>
        <button type="button" className={view === "mimovistar" ? "active" : ""} aria-pressed={view === "mimovistar"} onClick={() => void selectView("mimovistar")}>Mi Movistar</button>
        <button type="button" className={view === "guiado" ? "active" : ""} aria-pressed={view === "guiado"} onClick={() => void selectView("guiado")}>Recibo guiado</button>
        <button type="button" className={view === "conversacional" ? "active" : ""} aria-pressed={view === "conversacional"} onClick={() => void selectView("conversacional")}>IA conversacional</button>
        <button type="button" className={view === "asesor" ? "active" : ""} aria-pressed={view === "asesor"} onClick={() => void selectView("asesor")}>Asesor 104</button>
      </nav>
      <div className="topbar-status">
        {/* La sesión se cierra desde aquí, junto a la cuenta a la que pertenece. Antes
            vivía dentro de la pestaña «Login»; al fundir esa pestaña con Mi Movistar,
            quedó inalcanzable con la sesión abierta —el botón llevaba al asistente—. */}
        {account && <span className="signed-account">{account}</span>}
        {account && <button type="button" className="salir-sesion" onClick={() => void logout()}>Cerrar sesión</button>}
        <div className={`service ${health.isSuccess ? "online" : "offline"}`}><i />{health.isSuccess ? "API operativa" : "API sin conexión"}</div>
      </div>
    </header>
    {error && <div className="error-banner" role="alert">{error}</div>}

    {view === "whatsapp" ? <main className="canal-completo">
      {/* Sin panel lateral: es el canal del cliente, no una consola de auditoría. La
          garantía de «cero importes» se enseña dentro de la propia conversación. */}
      <WhatsApp cuentaSugerida={account} />
    </main> : view === "mimovistar" ? <main className="canal-completo">
      <MiMovistar
        key={sesionesCerradas}
        onSesion={(cuentaEntrada, tokenEmitido) => { setAccount(cuentaEntrada); setToken(tokenEmitido); }}
        onExplicacion={async (resultado) => {
          setExplanation(resultado);
          try { setAudit(await api.audit(token || "", resultado.trace_id)); } catch { setAudit(null); }
        }}
      />
    </main> : view === "guiado" ? <main className="canal-completo">
      {/* Misma app, con el recibo delante: cada frase enciende la línea de la que habla.
          La pantalla no interpreta el texto, lee los `fact_ids` que el motor ya puso. */}
      <ReciboGuiado
        key={sesionesCerradas}
        onSesion={(cuentaEntrada, tokenEmitido) => { setAccount(cuentaEntrada); setToken(tokenEmitido); }}
      />
    </main> : view === "asesor" ? <main className="canal-completo asesor-canal">
      <Asesor />
    </main> : !token ? <main className="canal-completo">
      {/* Sin sesión esta vista era un callejón: el chat no podía preguntar y el botón de
          voz salía deshabilitado sin decir por qué. El acceso vive en Mi Movistar —es
          donde el cliente se autentica— así que desde aquí se acompaña hasta allí en vez
          de dejar una pantalla muerta. */}
      <section className="panel sin-sesion">
        <p className="eyebrow">IA conversacional · Requiere sesión</p>
        <h2>Entre primero a Mi Movistar</h2>
        <p>
          Esta vista explica el recibo <b>con importes</b> y habilita la voz de Gemini
          Live, así que necesita una sesión autenticada (<code>LOA2</code>). Se abre en la
          pestaña <b>Mi Movistar</b> con el id del usuario.
        </p>
        <button onClick={() => void selectView("mimovistar")}>Ir a Mi Movistar</button>
      </section>
    </main> : <main>
      <div className="workspace">
        <section className="customer panel">
          <div className="ia-brand-row">
            <img src={billsenseLogo} alt="BillSense" className="ia-brand-logo" />
            <div><strong>BillSense</strong><span>Explicación verificada de tu recibo</span></div>
          </div>
          <div className="controls">
            <div className="account-identity"><small>Cuenta autenticada</small><strong>{account}</strong></div>
            <label>Detalle<select value={detail} onChange={(event) => setDetail(event.target.value)}><option>CORTO</option><option>DETALLE</option></select></label>
          </div>
          {facts && <div className="summary"><div><small>Anterior</small><strong>{money(facts.total_previo_cent)}</strong></div><div><small>Actual</small><strong>{money(facts.total_actual_cent)}</strong></div><div className={facts.delta_total_cent >= 0 ? "up" : "down"}><small>Variación</small><strong>{facts.delta_total_cent > 0 ? "+" : ""}{money(facts.delta_total_cent)}</strong></div><div><small>Periodo</small><strong>{facts.periodo_actual}</strong></div></div>}
          <div className="conversation">
            {turns.length === 0 && !explain.isPending && <div className="welcome"><img src={billsenseLogo} alt="BillSense" /><h2>Hola, soy su asistente de recibos</h2><p>Pregúnteme por qué cambió su recibo.</p></div>}
            {turns.map((turno, indice) => turno.rol === "cliente"
              ? <div className="turn client" key={`c-${indice}`}><p>{turno.texto}</p></div>
              : <div className="turn agent" key={`a-${indice}`}><Blocks blocks={turno.explicacion.bloques} /></div>)}
            {explain.isPending && <div className="turn agent"><div className="typing" role="status" aria-live="polite"><span /><span /><span /><small>Consultando su recibo y verificando cada cifra…</small></div></div>}
            <div ref={finDeLaConversacion} />
          </div>
          <div className="quick"><button onClick={() => setQuestion("¿Por qué me vino más caro este mes?")}>¿Por qué subió?</button><button onClick={() => setQuestion("¿Qué me están cobrando?")}>Ver cobros</button><button onClick={() => setQuestion("Quiero hablar con un asesor")}>Hablar con asesor</button></div>
          <form onSubmit={submit}><input aria-label="Consulta" value={question} maxLength={2000} onChange={(event) => setQuestion(event.target.value)} /><button disabled={!token || explain.isPending}>{explain.isPending ? "Verificando…" : "Explicar"}</button></form>
        </section>
        <aside className="governance panel"><div className="panel-heading"><div><p className="eyebrow">Gobernanza en tiempo real</p><h2>Cada cifra tiene respaldo</h2></div><span className={`verdict ${governance?.verificacion_numerica === "PASS" ? "pass" : "idle"}`}>{governance?.verificacion_numerica ?? "ESPERANDO"}</span></div>
          <div className="metrics"><div><strong>{governance?.aserciones_totales ?? "—"}</strong><small>Afirmaciones</small></div><div><strong>{governance?.aserciones_ancladas ?? "—"}</strong><small>Ancladas</small></div><div><strong>{governance?.aserciones_no_ancladas ?? "—"}</strong><small>Sin respaldo</small></div></div>
          <div className="model-card"><span>Modo de generación</span><strong>{governance?.modo ?? String(health.data?.llm_mode ?? "mock")}</strong><small>{governance?.model_version ?? "Esperando una explicación"}</small></div>
          {explanation?.derivacion.requerida && <div className="handoff"><strong>Derivación requerida</strong><p>{explanation.derivacion.motivo}</p></div>}
          <button className="danger" disabled={!token || adversarial.isPending} onClick={() => adversarial.mutate()}>Inyectar cifra adversaria</button>
          {/* Cerrado de entrada y con el resumen legible delante. Antes se abría solo
              (`open={Boolean(audit)}`) y volcaba el JSON íntegro de la traza: con el
              dataset real son ~620 líneas, que en esta columna tapaban la explicación y
              hacían parecer que la respuesta al cliente salía dentro de la auditoría. La
              traza es una PRUEBA que se consulta, no el mensaje que se lee. */}
          <details><summary>Auditoría del turno</summary>
            {auditLines.length > 0 && <pre className="audit-summary">{auditLines.join("\n")}</pre>}
            {audit
              ? <details><summary>Traza completa (JSON)</summary><pre>{JSON.stringify(audit, null, 2)}</pre></details>
              : <p className="audit-empty">La traza aparecerá después de la primera explicación.</p>}
          </details>
        </aside>
      </div>
    </main>}
  </div>;
}

function message(cause: unknown) {
  return cause instanceof ApiError ? `${cause.code}: ${cause.message}` : cause instanceof Error ? cause.message : "Ocurrió un error inesperado";
}
