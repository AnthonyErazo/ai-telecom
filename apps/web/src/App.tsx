import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./api/client";
import type { Explanation, FactSet } from "./api/types";
import { Blocks, money } from "./components/Blocks";
import { GeminiLiveClient, type LiveStatus } from "./live/client";
import { microphoneSupportError } from "./live/audio";

const fallback = ["C-DEMO-01", "C-DEMO-02", "C-DEMO-03"];
type View = "login" | "assistant";

export default function App() {
  const [view, setView] = useState<View>("login");
  const [account, setAccount] = useState("");
  const [accountDraft, setAccountDraft] = useState(fallback[0]);
  const [token, setToken] = useState("");
  const [facts, setFacts] = useState<FactSet | null>(null);
  const [question, setQuestion] = useState("¿Por qué me vino más caro este mes?");
  const [detail, setDetail] = useState("CORTO");
  const [explanation, setExplanation] = useState<Explanation | null>(null);
  const [audit, setAudit] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [liveStatus, setLiveStatus] = useState<LiveStatus>("idle");
  const [inputTranscript, setInputTranscript] = useState("");
  const [outputTranscript, setOutputTranscript] = useState("");
  const liveClient = useRef<GeminiLiveClient | null>(null);

  const health = useQuery({ queryKey: ["health"], queryFn: api.health, retry: 1 });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const accountList = accounts.data?.demo?.length ? accounts.data.demo : fallback;
  const microphoneIssue = microphoneSupportError();

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
      setAudit(null);
      setError("");
      setView("assistant");
    },
    onError: (cause) => setError(message(cause)),
  });

  useEffect(() => () => { void liveClient.current?.close(); }, []);

  const explain = useMutation({
    mutationFn: () => api.explain(token, {
      conversation_id: explanation?.conversation_id,
      cuenta_id: account,
      verbosidad: detail,
      utterance: question,
    }),
    onSuccess: async (result) => {
      setExplanation(result);
      setError("");
      try { setAudit(await api.audit(token, result.trace_id)); } catch { setAudit(null); }
    },
    onError: (cause) => setError(message(cause)),
  });

  const adversarial = useMutation({
    mutationFn: () => api.hallucinate(token, account),
    onSuccess: (result) => setAudit(result),
    onError: (cause) => setError(message(cause)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (token && question.trim()) explain.mutate();
  };

  const login = (event: FormEvent) => {
    event.preventDefault();
    const next = accountDraft.trim();
    if (next) bootstrap.mutate(next);
  };

  const stopLive = async () => {
    if (!liveClient.current) return;
    const client = liveClient.current;
    liveClient.current = null;
    await client.close();
  };

  const selectView = async (next: View) => {
    if (next === "assistant" && !token) return;
    if (next === "login") await stopLive();
    setView(next);
  };

  const logout = async () => {
    await stopLive();
    setToken("");
    setAccount("");
    setFacts(null);
    setExplanation(null);
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
  return <div className="shell">
    <header className="topbar">
      <a className="brand" href="/ui/"><span>RC</span><div>recibo claro<small>IA financiera verificable</small></div></a>
      <nav className="app-tabs" aria-label="Navegación principal">
        <button type="button" className={view === "login" ? "active" : ""} aria-pressed={view === "login"} onClick={() => void selectView("login")}>Login</button>
        <button type="button" className={view === "assistant" ? "active" : ""} aria-pressed={view === "assistant"} disabled={!token} onClick={() => void selectView("assistant")}>Asistente</button>
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
          <input id="login-account" list="account-options" autoComplete="username" autoFocus value={accountDraft} onChange={(event) => setAccountDraft(event.target.value)} placeholder="Ej. C-DEMO-01" />
          <datalist id="account-options">{accountList.map((id) => <option key={id} value={id} />)}</datalist>
          <button type="submit" disabled={!accountDraft.trim() || bootstrap.isPending}>{bootstrap.isPending ? "Validando…" : "Ingresar"}</button>
          <small>Acceso de demostración: usa <code>/dev/token</code>. No solicita contraseña.</small>
        </form> : <div className="session-card">
          <span className="session-check">✓</span>
          <div><small>Sesión activa</small><strong>{account}</strong><p>El asistente y Gemini Live están habilitados para esta cuenta.</p></div>
          <div className="session-actions"><button type="button" className="primary" onClick={() => setView("assistant")}>Abrir asistente</button><button type="button" onClick={() => void logout()}>Cerrar sesión</button></div>
        </div>}
      </section>
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
          <div className="conversation">{!explanation && <div className="welcome"><span>✦</span><h2>Hola, soy Recibo Claro</h2><p>Selecciona una consulta o inicia una conversación de voz verificable.</p></div>}{explanation && <Blocks blocks={explanation.bloques} />}</div>
          <div className="quick"><button onClick={() => setQuestion("¿Por qué me vino más caro este mes?")}>¿Por qué subió?</button><button onClick={() => setQuestion("¿Qué me están cobrando?")}>Ver cobros</button><button onClick={() => setQuestion("Quiero hablar con un asesor")}>Hablar con asesor</button></div>
          {(inputTranscript || outputTranscript) && <div className="live-transcript" aria-live="polite">{inputTranscript && <p><strong>Usted:</strong> {inputTranscript}</p>}{outputTranscript && <p><strong>Gemini Live:</strong> {outputTranscript}</p>}</div>}
          <form onSubmit={submit}><input aria-label="Consulta" value={question} maxLength={2000} onChange={(event) => setQuestion(event.target.value)} /><button disabled={!token || explain.isPending}>{explain.isPending ? "Verificando…" : "Explicar"}</button></form>
        </section>
        <aside className="governance panel"><div className="panel-heading"><div><p className="eyebrow">Gobernanza en tiempo real</p><h2>Cada cifra tiene respaldo</h2></div><span className={`verdict ${governance?.verificacion_numerica === "PASS" ? "pass" : "idle"}`}>{governance?.verificacion_numerica ?? "ESPERANDO"}</span></div>
          <div className="metrics"><div><strong>{governance?.aserciones_totales ?? "—"}</strong><small>Afirmaciones</small></div><div><strong>{governance?.aserciones_ancladas ?? "—"}</strong><small>Ancladas</small></div><div><strong>{governance?.aserciones_no_ancladas ?? "—"}</strong><small>Sin respaldo</small></div></div>
          <div className="model-card"><span>Modo de generación</span><strong>{governance?.modo ?? String(health.data?.llm_mode ?? "mock")}</strong><small>{governance?.model_version ?? "Esperando una explicación"}</small></div>
          {explanation?.derivacion.requerida && <div className="handoff"><strong>Derivación requerida</strong><p>{explanation.derivacion.motivo}</p></div>}
          <button className="danger" disabled={!token || adversarial.isPending} onClick={() => adversarial.mutate()}>Inyectar cifra adversaria</button>
          <details open={Boolean(audit)}><summary>Auditoría del turno</summary><pre>{audit ? JSON.stringify(audit, null, 2) : "La traza aparecerá después de la primera explicación."}</pre></details>
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
