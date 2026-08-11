import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./api/client";
import type { Explanation, FactSet } from "./api/types";
import { Blocks, money } from "./components/Blocks";

const fallback = ["C-DEMO-01", "C-DEMO-02", "C-DEMO-03"];

export default function App() {
  const [account, setAccount] = useState(fallback[0]);
  const [token, setToken] = useState("");
  const [facts, setFacts] = useState<FactSet | null>(null);
  const [question, setQuestion] = useState("¿Por qué me vino más caro este mes?");
  const [detail, setDetail] = useState("CORTO");
  const [explanation, setExplanation] = useState<Explanation | null>(null);
  const [audit, setAudit] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");

  const health = useQuery({ queryKey: ["health"], queryFn: api.health, retry: 1 });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts, retry: 1 });
  const accountList = accounts.data?.demo?.length ? accounts.data.demo : fallback;

  const bootstrap = useMutation({
    mutationFn: async (id: string) => {
      const issued = await api.token(id);
      const loadedFacts = await api.facts(issued.access_token);
      return { token: issued.access_token, facts: loadedFacts };
    },
    onSuccess: (data) => { setToken(data.token); setFacts(data.facts); setExplanation(null); setAudit(null); setError(""); },
    onError: (cause) => setError(message(cause))
  });

  useEffect(() => { bootstrap.mutate(account); }, [account]); // eslint-disable-line react-hooks/exhaustive-deps

  const explain = useMutation({
    mutationFn: () => api.explain(token, { conversation_id: explanation?.conversation_id, cuenta_id: account, verbosidad: detail, utterance: question }),
    onSuccess: async (result) => {
      setExplanation(result); setError("");
      try { setAudit(await api.audit(token, result.trace_id)); } catch { setAudit(null); }
    },
    onError: (cause) => setError(message(cause))
  });

  const adversarial = useMutation({
    mutationFn: () => api.hallucinate(token, account),
    onSuccess: (result) => setAudit(result),
    onError: (cause) => setError(message(cause))
  });

  const submit = (event: FormEvent) => { event.preventDefault(); if (token && question.trim()) explain.mutate(); };
  const governance = explanation?.gobernanza;

  return <div className="shell">
    <header className="topbar"><a className="brand" href="/ui/"><span>RC</span><div>recibo claro<small>IA financiera verificable</small></div></a><div className={`service ${health.isSuccess ? "online" : "offline"}`}><i />{health.isSuccess ? "API operativa" : "API sin conexión"}</div></header>
    {error && <div className="error-banner" role="alert">{error}</div>}
    <main>
      <section className="hero"><p className="eyebrow">Explicación inteligente · Cero cifras inventadas</p><h1>Entiende qué cambió en tu recibo.</h1><p>El motor calcula; la IA explica; el verificador comprueba cada número antes de mostrarlo.</p></section>
      <div className="workspace">
        <section className="customer panel">
          <div className="controls"><label>Cuenta<select value={account} onChange={(e) => setAccount(e.target.value)}>{accountList.map((id) => <option key={id}>{id}</option>)}</select></label><label>Detalle<select value={detail} onChange={(e) => setDetail(e.target.value)}><option>CORTO</option><option>DETALLE</option></select></label></div>
          {facts && <div className="summary"><div><small>Anterior</small><strong>{money(facts.total_previo_cent)}</strong></div><div><small>Actual</small><strong>{money(facts.total_actual_cent)}</strong></div><div className={facts.delta_total_cent >= 0 ? "up" : "down"}><small>Variación</small><strong>{facts.delta_total_cent > 0 ? "+" : ""}{money(facts.delta_total_cent)}</strong></div><div><small>Periodo</small><strong>{facts.periodo_actual}</strong></div></div>}
          <div className="conversation">{!explanation && <div className="welcome"><span>✦</span><h2>Hola, soy Recibo Claro</h2><p>Selecciona una consulta para revisar el recibo con evidencia verificable.</p></div>}{explanation && <Blocks blocks={explanation.bloques} />}</div>
          <div className="quick"><button onClick={() => setQuestion("¿Por qué me vino más caro este mes?")}>¿Por qué subió?</button><button onClick={() => setQuestion("¿Qué me están cobrando?")}>Ver cobros</button><button onClick={() => setQuestion("Quiero hablar con un asesor")}>Hablar con asesor</button></div>
          <form onSubmit={submit}><input aria-label="Consulta" value={question} maxLength={2000} onChange={(e) => setQuestion(e.target.value)} /><button disabled={!token || explain.isPending}>{explain.isPending ? "Verificando…" : "Explicar"}</button></form>
        </section>
        <aside className="governance panel"><div className="panel-heading"><div><p className="eyebrow">Gobernanza en tiempo real</p><h2>Cada cifra tiene respaldo</h2></div><span className={`verdict ${governance?.verificacion_numerica === "PASS" ? "pass" : "idle"}`}>{governance?.verificacion_numerica ?? "ESPERANDO"}</span></div>
          <div className="metrics"><div><strong>{governance?.aserciones_totales ?? "—"}</strong><small>Afirmaciones</small></div><div><strong>{governance?.aserciones_ancladas ?? "—"}</strong><small>Ancladas</small></div><div><strong>{governance?.aserciones_no_ancladas ?? "—"}</strong><small>Sin respaldo</small></div></div>
          <div className="model-card"><span>Modo de generación</span><strong>{governance?.modo ?? String(health.data?.llm_mode ?? "mock")}</strong><small>{governance?.model_version ?? "Esperando una explicación"}</small></div>
          {explanation?.derivacion.requerida && <div className="handoff"><strong>Derivación requerida</strong><p>{explanation.derivacion.motivo}</p></div>}
          <button className="danger" disabled={!token || adversarial.isPending} onClick={() => adversarial.mutate()}>Inyectar cifra adversaria</button>
          <details open={Boolean(audit)}><summary>Auditoría del turno</summary><pre>{audit ? JSON.stringify(audit, null, 2) : "La traza aparecerá después de la primera explicación."}</pre></details>
        </aside>
      </div>
    </main>
    <footer>Recibo Claro · Las cifras se calculan en el backend y nunca en el navegador.</footer>
  </div>;
}

function message(cause: unknown) {
  return cause instanceof ApiError ? `${cause.code}: ${cause.message}` : cause instanceof Error ? cause.message : "Ocurrió un error inesperado";
}
