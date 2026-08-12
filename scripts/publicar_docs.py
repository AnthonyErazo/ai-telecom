"""Construye la documentación como **una sola página HTML autocontenida**.

Para qué
--------
No todo el equipo tiene GitHub, y el jurado va a mirar esto desde el móvil. Un repositorio
no es una forma de enseñar documentación a alguien que no programa: hay que crear cuenta,
entender qué es un `.md` y navegar quince ficheros sueltos. Esto produce un solo `.html`
que se abre con doble clic, se manda por WhatsApp y funciona sin conexión.

Por qué un fichero y no un sitio
--------------------------------
Un sitio estático necesita servidor, o al menos un `file://` con varias rutas relativas que
los navegadores móviles tratan de forma distinta. Un fichero único no tiene ese problema:
todo —texto, estilos, buscador— viaja dentro. También es lo que permite publicarlo como
artefacto sin depender de ningún CDN, que la política de seguridad bloquea de todos modos.

Qué NO hace
-----------
No renderiza Markdown en el navegador. La conversión se hace aquí, en Python, y lo que se
publica es HTML ya construido. Meter un parser de Markdown en la página añadiría 100 KB de
JavaScript para hacer en cada visita lo que se puede hacer una vez.

    python scripts/publicar_docs.py            # escribe docs/sitio/documentacion.html
"""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]

#: Orden de lectura, no alfabético. Quien abre esto por primera vez necesita saber qué es
#: el proyecto antes de leer por qué el recibo no se vectoriza.
ORDEN: tuple[tuple[str, str, str], ...] = (
    ("README.md", "Qué es recibo-claro", "Empezar"),
    ("docs/COMO_FUNCIONA.md", "Cómo funciona", "Empezar"),
    ("docs/COMO_PROBAR.md", "Cómo probarlo", "Empezar"),
    ("docs/arquitectura.md", "Arquitectura", "Diseño"),
    ("docs/BACKEND_Y_DATOS.md", "Backend y datos", "Diseño"),
    ("docs/ELECCION_DEL_MODELO.md", "Elección del modelo", "Diseño"),
    ("docs/FUNDAMENTACION.md", "Fundamentación", "Sustento"),
    ("docs/PROCEDENCIA.md", "Procedencia y herramientas", "Sustento"),
    ("docs/ADR/001-el-recibo-no-se-vectoriza.md", "001 · El recibo no se vectoriza", "Decisiones"),
    ("docs/ADR/002-montos-en-centimos-enteros.md", "002 · Montos en céntimos enteros", "Decisiones"),
    ("docs/ADR/003-el-llm-no-calcula.md", "003 · El LLM no calcula", "Decisiones"),
    ("docs/ADR/004-modelo-de-tramos.md", "004 · Modelo de tramos", "Decisiones"),
    ("docs/ADR/005-langgraph-para-la-orquestacion.md", "005 · LangGraph para la orquestación", "Decisiones"),
)


def _slug(texto: str) -> str:
    """Identificador estable para un título. Sin tildes ni signos."""
    import unicodedata

    plano = unicodedata.normalize("NFD", texto.lower())
    plano = "".join(c for c in plano if unicodedata.category(c) != "Mn")
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", plano)).strip("-") or "seccion"


def _convertir(ruta: Path, prefijo: str) -> tuple[str, list[tuple[int, str, str]]]:
    """Markdown a HTML, devolviendo también los títulos para el índice lateral.

    Los ``id`` llevan el prefijo del documento porque los quince comparten página: sin él,
    dos documentos con una sección «Métricas» generarían el mismo ancla y el índice
    saltaría al documento equivocado.
    """
    import markdown

    convertidor = markdown.Markdown(
        extensions=["extra", "sane_lists", "admonition"],
        output_format="html5",
    )
    cuerpo = convertidor.convert(ruta.read_text(encoding="utf-8"))

    indice: list[tuple[int, str, str]] = []

    def anclar(coincidencia: re.Match[str]) -> str:
        nivel = int(coincidencia.group(1))
        contenido = coincidencia.group(2)
        titulo = re.sub(r"<[^>]+>", "", contenido).strip()
        ancla = f"{prefijo}--{_slug(titulo)}"
        # Solo H2 y H3 van al índice: con los H4 el panel lateral pasaría de 300 entradas
        # y dejaría de servir para orientarse, que es lo único que tiene que hacer.
        if 2 <= nivel <= 3 and titulo:
            indice.append((nivel, ancla, titulo))
        return f'<h{nivel} id="{ancla}">{contenido}</h{nivel}>'

    cuerpo = re.sub(r"<h([1-6])>(.*?)</h\1>", anclar, cuerpo, flags=re.S)
    # Las tablas anchas necesitan su propio contenedor con scroll o el cuerpo de la página
    # se desplaza en horizontal, que en móvil arruina la lectura de todo lo demás.
    cuerpo = cuerpo.replace("<table>", '<div class="tabla"><table>').replace(
        "</table>", "</table></div>"
    )
    return cuerpo, indice


def _texto_plano(ruta: Path) -> str:
    """Texto sin marcas, para el buscador. Se recorta lo que no aporta a una búsqueda."""
    texto = ruta.read_text(encoding="utf-8")
    texto = re.sub(r"```.*?```", " ", texto, flags=re.S)   # los bloques de código, fuera
    texto = re.sub(r"[#*_`>|\-]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def construir() -> Path:
    """Genera el fichero y devuelve su ruta."""
    documentos: list[dict] = []
    for ruta_rel, titulo, seccion in ORDEN:
        ruta = RAIZ / ruta_rel
        if not ruta.is_file():
            print(f"  aviso: falta {ruta_rel}, se omite")
            continue
        prefijo = _slug(Path(ruta_rel).stem)
        cuerpo, indice = _convertir(ruta, prefijo)
        documentos.append(
            {
                "id": prefijo,
                "titulo": titulo,
                "seccion": seccion,
                "origen": ruta_rel,
                "cuerpo": cuerpo,
                "indice": indice,
                "texto": _texto_plano(ruta)[:200_000],
                "lineas": len(ruta.read_text(encoding="utf-8").splitlines()),
            }
        )

    pagina = _plantilla(documentos)
    destino = RAIZ / "docs" / "sitio" / "documentacion.html"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(pagina, encoding="utf-8")
    return destino


def _plantilla(documentos: list[dict]) -> str:
    """Ensambla la página. Todo el contenido va incrustado; no hay peticiones de red."""
    indice_busqueda = json.dumps(
        [{"id": d["id"], "t": d["titulo"], "x": d["texto"]} for d in documentos],
        ensure_ascii=False,
    )

    # -- panel lateral ------------------------------------------------------ #
    nav: list[str] = []
    seccion_actual = ""
    for doc in documentos:
        if doc["seccion"] != seccion_actual:
            seccion_actual = doc["seccion"]
            nav.append(f'<p class="nav-seccion">{html.escape(seccion_actual)}</p>')
        nav.append(
            f'<a class="nav-doc" href="#{doc["id"]}" data-doc="{doc["id"]}">'
            f'{html.escape(doc["titulo"])}'
            f'<span class="nav-lineas">{doc["lineas"]}</span></a>'
        )
        sub = "".join(
            f'<a class="nav-sub n{nivel}" href="#{ancla}">{html.escape(titulo)}</a>'
            for nivel, ancla, titulo in doc["indice"][:40]
        )
        if sub:
            nav.append(f'<div class="nav-subs" data-de="{doc["id"]}">{sub}</div>')

    # -- documentos --------------------------------------------------------- #
    articulos = "".join(
        f'<article class="doc" id="{d["id"]}" data-doc="{d["id"]}">'
        f'<header class="doc-cab"><p class="doc-seccion">{html.escape(d["seccion"])}</p>'
        f'<h1 class="doc-titulo">{html.escape(d["titulo"])}</h1>'
        f'<p class="doc-origen">{html.escape(d["origen"])} · {d["lineas"]} líneas</p></header>'
        f'{d["cuerpo"]}</article>'
        for d in documentos
    )

    total_lineas = sum(d["lineas"] for d in documentos)
    return _ESQUELETO.replace("{{NAV}}", "".join(nav)) \
                     .replace("{{DOCS}}", articulos) \
                     .replace("{{BUSQUEDA}}", indice_busqueda) \
                     .replace("{{N_DOCS}}", str(len(documentos))) \
                     .replace("{{N_LINEAS}}", f"{total_lineas:,}".replace(",", " "))


_ESQUELETO = r"""<title>recibo-claro · Documentación</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
/* ---------------------------------------------------------------------------
   Sistema de color. Se define ENTERO en :root (tema claro) y solo se redefinen
   los tokens en los dos bloques oscuros, para que la página resuelva también
   cuando el visor no estampa ningún atributo (ajuste «sistema»).
   La paleta es de libro mayor: tinta azul sobre papel frío, filete, y un solo
   acento. El rojo y el verde NO son decorativos: son el signo del delta.
--------------------------------------------------------------------------- */
:root {
  --papel:      #F6F7F9;
  --papel-alto: #FFFFFF;
  --papel-hund: #ECEFF3;
  --tinta:      #14213A;
  --tinta-2:    #4A5568;
  --tinta-3:    #7B8798;
  --filete:     #DCE1E8;
  --acento:     #0B6E63;
  --acento-sua: #E3F1EE;
  --sube:       #B42318;
  --baja:       #067647;
  --codigo-bg:  #EEF1F5;
  --sombra:     0 1px 2px rgba(20,33,58,.06), 0 8px 24px rgba(20,33,58,.05);
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, serif;
  --sans:  ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  --mono:  ui-monospace, "SF Mono", "Cascadia Mono", "JetBrains Mono", Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --papel:      #10141C;
    --papel-alto: #161B26;
    --papel-hund: #1C2230;
    --tinta:      #E4E9F1;
    --tinta-2:    #A3AEC2;
    --tinta-3:    #77839A;
    --filete:     #262E3D;
    --acento:     #4DBFAF;
    --acento-sua: #12312D;
    --sube:       #F97066;
    --baja:       #47CD89;
    --codigo-bg:  #1A2130;
    --sombra:     0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.25);
  }
}
:root[data-theme="dark"] {
  --papel:      #10141C;
  --papel-alto: #161B26;
  --papel-hund: #1C2230;
  --tinta:      #E4E9F1;
  --tinta-2:    #A3AEC2;
  --tinta-3:    #77839A;
  --filete:     #262E3D;
  --acento:     #4DBFAF;
  --acento-sua: #12312D;
  --sube:       #F97066;
  --baja:       #47CD89;
  --codigo-bg:  #1A2130;
  --sombra:     0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.25);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--papel);
  color: var(--tinta);
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.65;
  -webkit-text-size-adjust: 100%;
}

/* --- Barra superior ------------------------------------------------------ */
.barra {
  position: sticky; top: 0; z-index: 40;
  display: flex; align-items: center; gap: 12px;
  padding: 10px 16px;
  background: color-mix(in srgb, var(--papel) 88%, transparent);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--filete);
}
.marca { display: flex; align-items: baseline; gap: 8px; font-family: var(--serif); }
.marca b { font-size: 17px; font-weight: 600; letter-spacing: -.01em; }
.marca span {
  font-family: var(--mono); font-size: 11px; color: var(--tinta-3);
  letter-spacing: .04em; text-transform: uppercase;
}
.buscador { margin-left: auto; position: relative; flex: 0 1 320px; }
.buscador input {
  width: 100%; padding: 7px 12px 7px 32px;
  border: 1px solid var(--filete); border-radius: 7px;
  background: var(--papel-alto); color: var(--tinta);
  font-family: var(--sans); font-size: 14px;
}
.buscador input:focus { outline: 2px solid var(--acento); outline-offset: 1px; border-color: transparent; }
.buscador svg { position: absolute; left: 10px; top: 9px; width: 15px; height: 15px; stroke: var(--tinta-3); fill: none; stroke-width: 2; }
.boton {
  display: inline-flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; flex: none;
  border: 1px solid var(--filete); border-radius: 7px;
  background: var(--papel-alto); color: var(--tinta);
  cursor: pointer; padding: 0;
}
.boton:hover { border-color: var(--acento); color: var(--acento); }
.boton:focus-visible { outline: 2px solid var(--acento); outline-offset: 2px; }
.boton svg { width: 17px; height: 17px; stroke: currentColor; fill: none; stroke-width: 1.8; }
#menu { display: none; }

/* --- Estructura ---------------------------------------------------------- */
.marco { display: grid; grid-template-columns: 286px minmax(0,1fr); align-items: start; }
.lateral {
  position: sticky; top: 55px; height: calc(100vh - 55px);
  overflow-y: auto; overscroll-behavior: contain;
  padding: 20px 12px 60px 16px;
  border-right: 1px solid var(--filete);
}
.nav-seccion {
  margin: 20px 0 6px; padding: 0 8px;
  font-family: var(--mono); font-size: 10.5px; letter-spacing: .1em;
  text-transform: uppercase; color: var(--tinta-3);
}
.nav-seccion:first-child { margin-top: 0; }
.nav-doc {
  display: flex; align-items: baseline; gap: 8px;
  padding: 6px 8px; border-radius: 6px;
  color: var(--tinta); text-decoration: none; font-size: 14.5px; font-weight: 500;
}
.nav-doc:hover { background: var(--papel-hund); }
.nav-doc.activo { background: var(--acento-sua); color: var(--acento); font-weight: 600; }
.nav-lineas {
  margin-left: auto; font-family: var(--mono); font-size: 10.5px;
  color: var(--tinta-3); font-variant-numeric: tabular-nums; font-weight: 400;
}
.nav-subs { display: none; margin: 2px 0 8px; padding-left: 8px; border-left: 1px solid var(--filete); }
.nav-subs.abierto { display: block; }
.nav-sub {
  display: block; padding: 3px 8px; border-radius: 5px;
  color: var(--tinta-2); text-decoration: none; font-size: 13px; line-height: 1.4;
}
.nav-sub.n3 { padding-left: 20px; font-size: 12.5px; color: var(--tinta-3); }
.nav-sub:hover { color: var(--acento); background: var(--papel-hund); }

.lienzo { min-width: 0; padding: 0 24px 120px; }
.doc { display: none; max-width: 68ch; margin: 0 auto; padding-top: 34px; }
.doc.visible { display: block; }

/* --- Cabecera de documento ----------------------------------------------- */
.doc-cab { padding-bottom: 20px; margin-bottom: 30px; border-bottom: 2px solid var(--tinta); }
.doc-seccion {
  margin: 0 0 8px; font-family: var(--mono); font-size: 11px;
  letter-spacing: .12em; text-transform: uppercase; color: var(--acento);
}
.doc-titulo {
  margin: 0; font-family: var(--serif); font-size: clamp(28px, 5vw, 40px);
  font-weight: 600; line-height: 1.12; letter-spacing: -.02em; text-wrap: balance;
}
.doc-origen {
  margin: 10px 0 0; font-family: var(--mono); font-size: 11.5px;
  color: var(--tinta-3); font-variant-numeric: tabular-nums;
}

/* --- Texto --------------------------------------------------------------- */
.doc h1, .doc h2, .doc h3, .doc h4, .doc h5, .doc h6 {
  font-family: var(--serif); font-weight: 600; line-height: 1.22;
  letter-spacing: -.012em; text-wrap: balance; scroll-margin-top: 70px;
}
.doc h1 { font-size: 30px; margin: 52px 0 16px; }
.doc h2 {
  font-size: 25px; margin: 48px 0 14px;
  padding-bottom: 7px; border-bottom: 1px solid var(--filete);
}
.doc h3 { font-size: 19.5px; margin: 34px 0 10px; }
.doc h4 { font-size: 16.5px; margin: 26px 0 8px; color: var(--tinta-2); }
.doc p { margin: 0 0 16px; }
.doc ul, .doc ol { margin: 0 0 16px; padding-left: 24px; }
.doc li { margin-bottom: 5px; }
.doc li > ul, .doc li > ol { margin-top: 5px; }
.doc a { color: var(--acento); text-decoration-thickness: 1px; text-underline-offset: 2px; }
.doc strong { font-weight: 650; }
.doc hr { border: 0; border-top: 1px solid var(--filete); margin: 36px 0; }

.doc blockquote {
  margin: 20px 0; padding: 14px 18px;
  background: var(--papel-hund); border-left: 3px solid var(--acento);
  border-radius: 0 7px 7px 0; color: var(--tinta-2);
}
.doc blockquote p:last-child { margin-bottom: 0; }

.doc code {
  font-family: var(--mono); font-size: .875em;
  background: var(--codigo-bg); padding: 1.5px 5px; border-radius: 4px;
  overflow-wrap: break-word;
}
.doc pre {
  margin: 20px 0; padding: 15px 17px; overflow-x: auto;
  background: var(--codigo-bg); border: 1px solid var(--filete); border-radius: 9px;
  font-size: 13px; line-height: 1.55;
}
.doc pre code { background: none; padding: 0; font-size: inherit; }

.tabla { overflow-x: auto; margin: 22px 0; border: 1px solid var(--filete); border-radius: 9px; }
.doc table { width: 100%; border-collapse: collapse; font-size: 14px; }
.doc th, .doc td {
  padding: 9px 13px; text-align: left; border-bottom: 1px solid var(--filete);
  vertical-align: top;
}
.doc thead th {
  background: var(--papel-hund); font-family: var(--mono);
  font-size: 11px; letter-spacing: .05em; text-transform: uppercase;
  color: var(--tinta-2); font-weight: 600; white-space: nowrap;
}
.doc tbody tr:last-child td { border-bottom: 0; }
/* Las columnas de importes se leen comparando dígitos: han de alinearse. */
.doc td { font-variant-numeric: tabular-nums; }

/* --- Buscador ------------------------------------------------------------ */
.resultados {
  display: none; max-width: 68ch; margin: 0 auto; padding-top: 34px;
}
.resultados.visible { display: block; }
.res-titulo { font-family: var(--serif); font-size: 26px; margin: 0 0 4px; font-weight: 600; }
.res-cuenta { font-family: var(--mono); font-size: 12px; color: var(--tinta-3); margin: 0 0 24px; }
.res {
  display: block; padding: 14px 16px; margin-bottom: 10px;
  background: var(--papel-alto); border: 1px solid var(--filete); border-radius: 9px;
  text-decoration: none; color: inherit; box-shadow: var(--sombra);
}
.res:hover { border-color: var(--acento); }
.res b { display: block; font-family: var(--serif); font-size: 16.5px; margin-bottom: 4px; color: var(--acento); }
.res span { font-size: 13.5px; color: var(--tinta-2); line-height: 1.55; }
.res mark { background: var(--acento-sua); color: inherit; padding: 0 2px; border-radius: 3px; font-weight: 600; }

/* --- Pie ----------------------------------------------------------------- */
.pie {
  max-width: 68ch; margin: 60px auto 0; padding-top: 22px;
  border-top: 1px solid var(--filete);
  font-family: var(--mono); font-size: 11.5px; color: var(--tinta-3);
  display: flex; flex-wrap: wrap; gap: 6px 18px;
}

/* --- Móvil ---------------------------------------------------------------- */
.velo { display: none; }
@media (max-width: 900px) {
  .marco { grid-template-columns: minmax(0,1fr); }
  #menu { display: inline-flex; }
  .lateral {
    position: fixed; top: 0; left: 0; bottom: 0; z-index: 60;
    width: min(300px, 86vw); height: 100%;
    background: var(--papel-alto); border-right: 1px solid var(--filete);
    padding-top: 18px;
    transform: translateX(-101%); transition: transform .22s ease;
  }
  .lateral.abierto { transform: none; box-shadow: 0 0 40px rgba(0,0,0,.28); }
  .velo {
    display: block; position: fixed; inset: 0; z-index: 50;
    background: rgba(10,16,26,.45); opacity: 0; pointer-events: none;
    transition: opacity .22s ease;
  }
  .velo.visible { opacity: 1; pointer-events: auto; }
  .lienzo { padding: 0 18px 100px; }
  .marca span { display: none; }
  .buscador { flex: 1 1 auto; }
  .doc { padding-top: 26px; }
  .doc h2 { font-size: 22px; }
  .doc pre { font-size: 12px; }
}
@media (prefers-reduced-motion: reduce) {
  * { transition-duration: .01ms !important; animation-duration: .01ms !important; }
}
</style>

<header class="barra">
  <button class="boton" id="menu" aria-label="Abrir el índice">
    <svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16" stroke-linecap="round"/></svg>
  </button>
  <div class="marca"><b>recibo-claro</b><span>Documentación</span></div>
  <div class="buscador">
    <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5" stroke-linecap="round"/></svg>
    <input id="q" type="search" placeholder="Buscar en toda la documentación" aria-label="Buscar">
  </div>
  <button class="boton" id="tema" aria-label="Cambiar entre claro y oscuro">
    <svg viewBox="0 0 24 24" id="icono-tema"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" stroke-linejoin="round"/></svg>
  </button>
</header>

<div class="velo" id="velo"></div>
<div class="marco">
  <nav class="lateral" id="lateral">{{NAV}}</nav>
  <main class="lienzo">
    <div class="resultados" id="resultados"></div>
    {{DOCS}}
    <footer class="pie">
      <span>{{N_DOCS}} documentos</span><span>{{N_LINEAS}} líneas</span>
      <span>Hackathon AI Telecom 2026 · Desafío 1</span>
    </footer>
  </main>
</div>

<script>
(function () {
  "use strict";
  const CORPUS = {{BUSQUEDA}};
  const $ = (s) => document.querySelector(s);
  const docs = document.querySelectorAll(".doc");
  const lateral = $("#lateral"), velo = $("#velo"), resultados = $("#resultados");

  /* -- Tema. La preferencia manual gana sobre la del sistema y se recuerda. -- */
  const guardado = localStorage.getItem("tema-recibo-claro");
  if (guardado) document.documentElement.setAttribute("data-theme", guardado);
  $("#tema").addEventListener("click", () => {
    const oscuroAhora = document.documentElement.getAttribute("data-theme") === "dark" ||
      (!document.documentElement.hasAttribute("data-theme") &&
       matchMedia("(prefers-color-scheme: dark)").matches);
    const nuevo = oscuroAhora ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", nuevo);
    localStorage.setItem("tema-recibo-claro", nuevo);
  });

  /* -- Cajón lateral en móvil -------------------------------------------- */
  function cajon(abierto) {
    lateral.classList.toggle("abierto", abierto);
    velo.classList.toggle("visible", abierto);
  }
  $("#menu").addEventListener("click", () => cajon(!lateral.classList.contains("abierto")));
  velo.addEventListener("click", () => cajon(false));

  /* -- Mostrar un documento ----------------------------------------------- */
  function mostrar(id, ancla) {
    const destino = document.getElementById(id) ? id : docs[0].dataset.doc;
    docs.forEach((d) => d.classList.toggle("visible", d.dataset.doc === destino));
    resultados.classList.remove("visible");
    document.querySelectorAll(".nav-doc").forEach((a) =>
      a.classList.toggle("activo", a.dataset.doc === destino));
    // Solo se despliega el sumario del documento abierto: mostrarlos todos convertiría
    // el panel en una lista de 300 entradas donde no se encuentra nada.
    document.querySelectorAll(".nav-subs").forEach((s) =>
      s.classList.toggle("abierto", s.dataset.de === destino));
    if (ancla) {
      const nodo = document.getElementById(ancla);
      if (nodo) { nodo.scrollIntoView(); return; }
    }
    window.scrollTo(0, 0);
  }

  function desdeHash() {
    const bruto = decodeURIComponent(location.hash.slice(1));
    if (!bruto) { mostrar(docs[0].dataset.doc); return; }
    // Un ancla de sección es «documento--seccion»: el documento se deduce del prefijo,
    // así que un enlace profundo abre la página correcta sin más metadatos.
    const doc = bruto.includes("--") ? bruto.split("--")[0] : bruto;
    mostrar(doc, bruto.includes("--") ? bruto : null);
  }
  addEventListener("hashchange", desdeHash);
  desdeHash();

  document.querySelectorAll(".nav-doc, .nav-sub").forEach((a) =>
    a.addEventListener("click", () => cajon(false)));

  /* -- Buscador ----------------------------------------------------------- */
  const sinTildes = (t) => t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const escapar = (t) => t.replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const indice = CORPUS.map((d) => ({ ...d, plano: sinTildes(d.x) }));
  let temporizador;

  $("#q").addEventListener("input", (ev) => {
    clearTimeout(temporizador);
    temporizador = setTimeout(() => buscar(ev.target.value.trim()), 130);
  });

  function buscar(consulta) {
    if (consulta.length < 2) { resultados.classList.remove("visible"); desdeHash(); return; }
    const aguja = sinTildes(consulta);
    const hallados = [];
    for (const doc of indice) {
      let desde = 0, veces = 0;
      const trozos = [];
      while (veces < 3) {
        const pos = doc.plano.indexOf(aguja, desde);
        if (pos === -1) break;
        veces++; desde = pos + aguja.length;
        // Se corta por palabra entera: partir a mitad de palabra hace que el extracto
        // parezca un error de codificación.
        const ini = Math.max(0, doc.x.lastIndexOf(" ", pos - 90) + 1);
        const fin = Math.min(doc.x.length, doc.x.indexOf(" ", pos + aguja.length + 90));
        const bruto = doc.x.slice(ini, fin === -1 ? undefined : fin);
        const rel = pos - ini;
        trozos.push(
          escapar(bruto.slice(0, rel)) + "<mark>" +
          escapar(bruto.slice(rel, rel + aguja.length)) + "</mark>" +
          escapar(bruto.slice(rel + aguja.length))
        );
      }
      if (veces) hallados.push({ id: doc.id, titulo: doc.t, veces, trozos });
    }
    hallados.sort((a, b) => b.veces - a.veces);

    docs.forEach((d) => d.classList.remove("visible"));
    resultados.classList.add("visible");
    if (!hallados.length) {
      resultados.innerHTML =
        '<h2 class="res-titulo">Sin resultados</h2>' +
        '<p class="res-cuenta">Nada coincide con «' + escapar(consulta) + '».</p>';
      return;
    }
    const total = hallados.reduce((n, h) => n + h.veces, 0);
    resultados.innerHTML =
      '<h2 class="res-titulo">Resultados</h2>' +
      '<p class="res-cuenta">' + total + ' coincidencias en ' + hallados.length +
      ' documento' + (hallados.length === 1 ? "" : "s") + '</p>' +
      hallados.map((h) =>
        '<a class="res" href="#' + h.id + '"><b>' + escapar(h.titulo) + '</b><span>…' +
        h.trozos.join(" … ") + '…</span></a>').join("");
    resultados.querySelectorAll(".res").forEach((a) =>
      a.addEventListener("click", () => { $("#q").value = ""; }));
    window.scrollTo(0, 0);
  }

  // La barra ya tiene un buscador; «/» lo enfoca sin quitar la mano del teclado.
  addEventListener("keydown", (ev) => {
    if (ev.key === "/" && document.activeElement !== $("#q")) { ev.preventDefault(); $("#q").focus(); }
    if (ev.key === "Escape") { $("#q").blur(); cajon(false); }
  });
})();
</script>
"""


if __name__ == "__main__":
    ruta = construir()
    print(f"  {ruta.relative_to(RAIZ)} · {ruta.stat().st_size / 1024:.0f} KB")
    sys.exit(0)
