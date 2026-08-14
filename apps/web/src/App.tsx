import { FormEvent, useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./api/client";
import type { Explanation, FactSet } from "./api/types";
import { Blocks, money } from "./components/Blocks";
import { WhatsApp } from "./components/WhatsApp";
import { Asesor } from "./components/Asesor";
import { MiMovistar } from "./components/MiMovistar";
import { GeminiLiveClient, type LiveStatus } from "./live/client";
import { microphoneSupportError } from "./live/audio";

// Solo se usan si `/dev/cuentas` no contesta. Son las cuentas del dataset sintético, así
// que dependen de que la API esté sirviendo el disco: cuando sirve Supabase, las cuentas
// buenas llegan de la API y estas no valen. Por eso NO se precarga ninguna en el campo
// —hacerlo dejaba «C-DEMO-01» escrito de entrada y, contra el dataset real, entrar sin
// tocar nada fallaba con «la cuenta no existe»—.
const fallback = ["C-DEMO-01", "C-DEMO-02", "C-DEMO-03"];
type View = "whatsapp" | "mimovistar";

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
  const [explanation, setExplanation] = useState<Explanation | null>(null);
  const [audit, setAudit] = useState<Record<string, unknown> | null>(null);
  // Cambia SOLO al cerrar sesión, y se usa como `key` de Mi Movistar para remontarla.
  // Atarla a la cuenta no servía: entrar también cambia la cuenta, y el remontaje
  // borraba la pantalla del recibo justo después de conseguirla.
  const [sesionesCerradas, setSesionesCerradas] = useState(0);
  const [error, setError] = useState("");

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
      setExplanation(null);
      setAudit(null);
      setError("");
    },
    onError: (cause) => setError(message(cause)),
  });

  const adversarial = useMutation({
    onSuccess: (result) => setAudit(result),
    onError: (cause) => setError(message(cause)),
  });



  const login = (event: FormEvent) => {
    event.preventDefault();
    if (cuentaElegida) bootstrap.mutate(cuentaElegida);
  };

  const selectView = async (next: View) => {
    setView(next);
  };

  const logout = async () => {
    setSesionesCerradas((n) => n + 1);
    setToken("");
    setAccount("");
    setExplanation(null);
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
      <a className="brand" href="/ui/"><img src="https://cert-cdn.movistar.com.pe/2024/12/logo-2.svg" alt="Movistar" height="40" style={{ width: "auto" }} /><div>MOVISTAR<small>App Mi Movistar</small></div></a>
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
        <button type="button" className={view === "mimovistar" ? "active" : ""} aria-pressed={view === "mimovistar"} onClick={() => void selectView("mimovistar")}>App Mi Movistar</button>
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
    </main> : <main>
      <div className="workspace">
        <section className="customer panel" style={{ background: "transparent", border: "none", padding: 0 }}>
          <MiMovistar
            key={sesionesCerradas}
            onSesion={(cuentaEntrada, tokenEmitido) => { setAccount(cuentaEntrada); setToken(tokenEmitido); }}
            onExplicacion={async (resultado) => {
              setExplanation(resultado);
              let currentToken = token;
              if (!currentToken && (account || cuentaEntrada)) {
                try {
                  const res = await api.token(account || cuentaEntrada);
                  currentToken = res.access_token;
                } catch (e) {
                  // ignore
                }
              }
              try { setAudit(await api.audit(currentToken || "", resultado.trace_id)); } catch { setAudit(null); }
            }}
          />
        </section>
        {token && <aside className="governance panel">
          <div className="panel-heading">
            <div><p className="eyebrow">Gobernanza en tiempo real</p><h2>Cada cifra tiene respaldo</h2></div>
            <span className={`verdict ${governance?.verificacion_numerica === "PASS" ? "pass" : "idle"}`}>{governance?.verificacion_numerica ?? "ESPERANDO"}</span>
          </div>
          <div className="metrics">
            <div><strong>{governance?.aserciones_totales ?? "—"}</strong><small>Afirmaciones</small></div>
            <div><strong>{governance?.aserciones_ancladas ?? "—"}</strong><small>Ancladas</small></div>
            <div><strong>{governance?.aserciones_no_ancladas ?? "—"}</strong><small>Sin respaldo</small></div>
          </div>
          <div className="model-card">
            <span>Modo de generación</span><strong>{governance?.modo ?? String(health.data?.llm_mode ?? "mock")}</strong>
            <small>{governance?.model_version ?? "Esperando una explicación"}</small>
          </div>
          {explanation?.derivacion.requerida && <div className="handoff"><strong>Derivación requerida</strong><p>{explanation.derivacion.motivo}</p></div>}
          <button className="danger" disabled={!token || adversarial.isPending} onClick={() => adversarial.mutate()}>Inyectar cifra adversaria</button>
          
          <details><summary>Auditoría del turno</summary>
            {auditLines.length > 0 && <pre className="audit-summary">{auditLines.join("\n")}</pre>}
            {audit
              ? <details><summary>Traza completa (JSON)</summary><pre>{JSON.stringify(audit, null, 2)}</pre></details>
              : <p className="audit-empty">La traza aparecerá después de la primera explicación.</p>}
          </details>
        </aside>}
      </div>
    </main>}
    <footer>BillSense · Las cifras se calculan en el backend y nunca en el navegador.</footer>
  </div>;
}

function message(cause: unknown) {
  return cause instanceof ApiError ? `${cause.code}: ${cause.message}` : cause instanceof Error ? cause.message : "Ocurrió un error inesperado";
}
