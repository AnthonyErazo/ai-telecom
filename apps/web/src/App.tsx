import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./api/client";
import type { Explanation, FactSet } from "./api/types";
import { Blocks, money } from "./components/Blocks";
import { WhatsApp } from "./components/WhatsApp";
import { Asesor } from "./components/Asesor";
import { GeminiLiveClient, type LiveStatus } from "./live/client";
import { microphoneSupportError } from "./live/audio";

// Solo se usan si `/dev/cuentas` no contesta. Son las cuentas del dataset sintético, así
// que dependen de que la API esté sirviendo el disco: cuando sirve Supabase, las cuentas
// buenas llegan de la API y estas no valen. Por eso NO se precarga ninguna en el campo
// —hacerlo dejaba «C-DEMO-01» escrito de entrada y, contra el dataset real, entrar sin
// tocar nada fallaba con «la cuenta no existe»—.
const fallback = ["C-DEMO-01", "C-DEMO-02", "C-DEMO-03"];
type View = "login" | "assistant" | "whatsapp" | "asesor";

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
  const [view, setView] = useState<View>("login");
  const [account, setAccount] = useState("");
  const [accountDraft, setAccountDraft] = useState("");
  const [token, setToken] = useState("");
  const [facts, setFacts] = useState<FactSet | null>(null);
  const [question, setQuestion] = useState("¿Por qué me vino más caro este mes?");
  const [detail, setDetail] = useState("CORTO");
  const [explanation, setExplanation] = useState<Explanation | null>(null);
  const [turns, setTurns] = useState<Turno[]>([]);
  const [audit, setAudit] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("idle");
  const [inputTranscript, setInputTranscript] = useState("");
  const [outputTranscript, setOutputTranscript] = useState("");
  const liveClient = useRef<GeminiLiveClient | null>(null);
  const finDeLaConversacion = useRef<HTMLDivElement | null>(null);

  const health = useQuery({ queryKey: ["health"], queryFn: api.health, retry: 1 });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const accountList = accounts.data?.demo?.length ? accounts.data.demo : fallback;
  const microphoneIssue = microphoneSupportError();

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
      setView("assistant");
    },
    onError: (cause) => setError(message(cause)),
  });

  useEffect(() => () => { void liveClient.current?.close(); }, []);

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

  const stopLive = async () => {
    if (!liveClient.current) return;
    const client = liveClient.current;
    liveClient.current = null;
    await client.close();
  };

  const selectView = async (next: View) => {
    if (next === "assistant" && !token) return;
    if (next === "whatsapp" && !account) return;
    // La voz pertenece a la vista de asistente: al salir de ella se corta, o seguiría
    // escuchando el micrófono desde una pantalla que ya no la ofrece.
    if (next !== "assistant") await stopLive();
    setView(next);
  };

  const logout = async () => {
    await stopLive();
    setToken("");
    setAccount("");
    setFacts(null);
    setExplanation(null);
    setTurns([]);
    setAudit(null);
    setInputTranscript("");
    setOutputTranscript("");
    setError("");
    setLiveStatus("idle");
    setView("login");
  };

  const toggleLive = async () => {
    if (liveClient.current) {
      await stopLive();
      return;
    }
    if (!token || !account) return;

    setInputTranscript("");
    setOutputTranscript("");
    setError("");
    const client = new GeminiLiveClient(
      { authToken: token, accountId: account, conversationId: explanation?.conversation_id, detail },
      {
        onStatus: setLiveStatus,
        onInputTranscript: (text) => setInputTranscript((current) => `${current} ${text}`.trim()),
        onOutputTranscript: (text) => setOutputTranscript((current) => `${current} ${text}`.trim()),
        onExplanation: (result) => {
          setExplanation(result);
          void api.audit(token, result.trace_id).then(setAudit).catch(() => setAudit(null));
        },
        onError: setError,
      },
    );
    liveClient.current = client;
    try {
      await client.connect();
    } catch (cause) {
      liveClient.current = null;
      setError(message(cause));
      await client.close();
      setLiveStatus("error");
    }
  };

  const governance = explanation?.gobernanza;
  // `GET /v1/auditoria` ya publica el mismo resumen de seis líneas que el proyecto
  // imprime en la terminal: veredicto, afirmaciones ancladas y una línea por etapa. Es
  // lo que un humano puede leer de un vistazo, así que es lo que se enseña; el volcado
  // completo queda debajo, para quien vaya a auditar de verdad.
  const auditLines = Array.isArray(audit?.terminal) ? (audit.terminal as string[]) : [];
  return <div className="shell">
    <header className="topbar">
      <a className="brand" href="/ui/"><span>RC</span><div>recibo claro<small>IA financiera verificable</small></div></a>
      <nav className="app-tabs" aria-label="Navegación principal">
        <button type="button" className={view === "login" ? "active" : ""} aria-pressed={view === "login"} onClick={() => void selectView("login")}>Login</button>
        <button type="button" className={view === "assistant" ? "active" : ""} aria-pressed={view === "assistant"} disabled={!token} onClick={() => void selectView("assistant")}>Asistente</button>
        {/* Los otros dos canales de la ficha. WhatsApp necesita una cuenta —emite su
            propio token LOA1—; la consola del asesor no, porque el asesor se identifica
            él mismo y declara a nombre de quién actúa. */}
        <button type="button" className={view === "whatsapp" ? "active" : ""} aria-pressed={view === "whatsapp"} disabled={!account} onClick={() => void selectView("whatsapp")}>WhatsApp</button>
        <button type="button" className={view === "asesor" ? "active" : ""} aria-pressed={view === "asesor"} onClick={() => void selectView("asesor")}>Asesor 104</button>
      </nav>
      <div className="topbar-status">
        {account && <span className="signed-account">{account}</span>}
        <div className={`service ${health.isSuccess ? "online" : "offline"}`}><i />{health.isSuccess ? "API operativa" : "API sin conexión"}</div>
      </div>
    </header>
    {error && <div className="error-banner" role="alert">{error}</div>}

    {view === "login" ? <main className="login-main">
      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-copy">
          <p className="eyebrow">Acceso del cliente</p>
          <h1 id="login-title">Ingrese a su cuenta</h1>
          <p>La sesión identifica el recibo que puede consultar y habilita la conexión segura con Gemini Live.</p>
        </div>
        {!token ? <form className="login-form" onSubmit={login}>
          <label htmlFor="login-account">Cuenta de usuario</label>
          <input id="login-account" list="account-options" autoComplete="username" autoFocus value={accountDraft} onChange={(event) => setAccountDraft(event.target.value)} placeholder={`Ej. ${accountList[0] ?? "C-DEMO-01"}`} />
          <datalist id="account-options">{accountList.map((id) => <option key={id} value={id} />)}</datalist>
          <button type="submit" disabled={!cuentaElegida || bootstrap.isPending}>{bootstrap.isPending ? "Validando…" : "Ingresar"}</button>
          <small>Acceso de demostración: usa <code>/dev/token</code>. No solicita contraseña.</small>
        </form> : <div className="session-card">
          <span className="session-check">✓</span>
          <div><small>Sesión activa</small><strong>{account}</strong><p>El asistente y Gemini Live están habilitados para esta cuenta.</p></div>
          <div className="session-actions"><button type="button" className="primary" onClick={() => setView("assistant")}>Abrir asistente</button><button type="button" onClick={() => void logout()}>Cerrar sesión</button></div>
        </div>}
      </section>
    </main> : view === "whatsapp" ? <main>
      <section className="hero"><p className="eyebrow">Canal WhatsApp · Identidad LOA1</p><h1>El mismo motor, sin un solo importe.</h1><p>La misma respuesta que la App, servida como texto y redactada por nivel de aseguramiento.</p></section>
      <WhatsApp cuenta={account} />
    </main> : view === "asesor" ? <main>
      <section className="hero"><p className="eyebrow">Call center 104 · Canal ASESOR</p><h1>El asesor no empieza de cero.</h1><p>El expediente se reconstruye desde la bitácora encadenada, con lo que no se pudo confirmar por delante.</p></section>
      <Asesor cuentaSugerida={account || cuentaElegida} />
    </main> : <main>
      <section className="hero"><p className="eyebrow">Explicación inteligente · Cero cifras inventadas</p><h1>Entiende qué cambió en tu recibo.</h1><p>El motor calcula; la IA explica; el verificador comprueba cada número antes de mostrarlo.</p></section>
      <div className="workspace">
        <section className="customer panel">
          <div className="controls">
            <div className="account-identity"><small>Cuenta autenticada</small><strong>{account}</strong></div>
            <label>Detalle<select value={detail} onChange={(event) => setDetail(event.target.value)}><option>CORTO</option><option>DETALLE</option></select></label>
            <div className="live-control"><button type="button" className={liveClient.current ? "live-stop" : "live-start"} title={microphoneIssue ?? undefined} disabled={!token || Boolean(microphoneIssue) || bootstrap.isPending || liveStatus === "connecting"} onClick={() => void toggleLive()}>{liveClient.current ? "Detener voz" : "Hablar con Gemini Live"}</button><small title={microphoneIssue ?? undefined}>{microphoneIssue ? "Requiere HTTPS o localhost" : liveLabel(liveStatus)}</small></div>
          </div>
          {facts && <div className="summary"><div><small>Anterior</small><strong>{money(facts.total_previo_cent)}</strong></div><div><small>Actual</small><strong>{money(facts.total_actual_cent)}</strong></div><div className={facts.delta_total_cent >= 0 ? "up" : "down"}><small>Variación</small><strong>{facts.delta_total_cent > 0 ? "+" : ""}{money(facts.delta_total_cent)}</strong></div><div><small>Periodo</small><strong>{facts.periodo_actual}</strong></div></div>}
          <div className="conversation">
            {turns.length === 0 && !explain.isPending && <div className="welcome"><span>✦</span><h2>Hola, soy Recibo Claro</h2><p>Pregúnteme por qué cambió su recibo, o inicie una conversación de voz verificable.</p></div>}
            {turns.map((turno, indice) => turno.rol === "cliente"
              ? <div className="turn client" key={`c-${indice}`}><p>{turno.texto}</p></div>
              : <div className="turn agent" key={`a-${indice}`}><Blocks blocks={turno.explicacion.bloques} /></div>)}
            {explain.isPending && <div className="turn agent"><div className="typing" role="status" aria-live="polite"><span /><span /><span /><small>Consultando su recibo y verificando cada cifra…</small></div></div>}
            <div ref={finDeLaConversacion} />
          </div>
          <div className="quick"><button onClick={() => setQuestion("¿Por qué me vino más caro este mes?")}>¿Por qué subió?</button><button onClick={() => setQuestion("¿Qué me están cobrando?")}>Ver cobros</button><button onClick={() => setQuestion("Quiero hablar con un asesor")}>Hablar con asesor</button></div>
          {(inputTranscript || outputTranscript) && <div className="live-transcript" aria-live="polite">{inputTranscript && <p><strong>Usted:</strong> {inputTranscript}</p>}{outputTranscript && <p><strong>Gemini Live:</strong> {outputTranscript}</p>}</div>}
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
    <footer>Recibo Claro · Las cifras se calculan en el backend y nunca en el navegador.</footer>
  </div>;
}

function message(cause: unknown) {
  return cause instanceof ApiError ? `${cause.code}: ${cause.message}` : cause instanceof Error ? cause.message : "Ocurrió un error inesperado";
}

function liveLabel(status: LiveStatus) {
  return ({ idle: "Voz inactiva", connecting: "Conectando…", listening: "Escuchando", consulting: "Consultando recibo", speaking: "Respondiendo", error: "Error de voz" })[status];
}
