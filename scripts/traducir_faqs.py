"""Traduce al español peruano las FAQs del corpus externo, directamente en Supabase.

    python scripts/traducir_faqs.py            # traduce lo que quede en inglés
    python scripts/traducir_faqs.py --limite 40

Por qué traducir y no buscar corpus en español
----------------------------------------------
Se buscó: no existe corpus de atención al cliente de telecomunicaciones en español con
licencia utilizable. El único del dominio está bajo GPL-3.0, prohibida por BASES §9 al
cederse la propiedad intelectual a Integratel. Traducir un corpus permisivo es la
alternativa honesta; inventarse las preguntas, no.

La marca ``traducida`` no es decorativa
---------------------------------------
Cada fila traducida queda con ``traducida = true`` e ``idioma = 'es'``. Nadie debe poder
confundir una FAQ traducida del inglés con una pregunta recogida en Perú: son cosas
distintas y la diferencia importa si alguien evalúa el sistema con datos reales.

Qué NO hace
-----------
No traduce «al español» a secas: pide **español peruano de atención al cliente**, con
«recibo» en vez de «factura» y trato de usted. Un corpus traducido a español neutro
sonaría a otro país y el recuperador acabaría casando preguntas que nadie escribe aquí.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parents[1]
# El script se ejecuta como fichero suelto, así que la raíz del proyecto no está en
# sys.path y `packages` no se importa. Se añade aquí y no con PYTHONPATH para que
# funcione igual desde cualquier directorio.
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

#: Cuántas FAQs por llamada. Suficientes para que salga a cuenta y pocas para que un
#: fallo no eche a perder mucho trabajo ni desborde el presupuesto de salida.
POR_LOTE = 20

ESQUEMA = {
    "type": "object",
    "required": ["traducciones"],
    "properties": {
        "traducciones": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["faq_id", "pregunta", "respuesta"],
                "properties": {
                    "faq_id": {"type": "string"},
                    "pregunta": {"type": "string"},
                    "respuesta": {"type": "string"},
                },
            },
        }
    },
}

INSTRUCCION = """Traduce al ESPAÑOL PERUANO de atención al cliente de telecomunicaciones.

Reglas:
- Trato de USTED, nunca de tú.
- Di «recibo», nunca «factura»: es como lo llama Movistar Perú.
- Registro natural de un asesor peruano: cercano pero correcto, sin regionalismos forzados.
- La PREGUNTA la escribe un cliente: si el original tiene erratas o es informal, consérvalo
  en la traducción. Es lo que hace útil este corpus.
- La RESPUESTA la escribe la operadora: correcta y clara.
- No añadas cifras, importes ni datos que no estén en el original.
- Devuelve el mismo faq_id que recibes.

FAQs a traducir:
"""


def _cargar_env() -> None:
    ruta = RAIZ / ".env"
    if not ruta.exists():
        return
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        if "=" in linea and not linea.strip().startswith("#"):
            clave, valor = linea.split("=", 1)
            os.environ.setdefault(clave.strip(), valor.strip())


def _rest(ruta: str, *, metodo: str = "GET", cuerpo: Any = None, extra: dict | None = None) -> tuple[int, str]:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    clave = os.getenv("SUPABASE_SECRET_KEY") or ""
    cabeceras = {"apikey": clave, "Authorization": f"Bearer {clave}", "Content-Type": "application/json"}
    cabeceras.update(extra or {})
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    peticion = urllib.request.Request(f"{url}/rest/v1/{ruta}", data=datos, headers=cabeceras, method=metodo)
    try:
        with urllib.request.urlopen(peticion, timeout=120) as respuesta:
            return respuesta.status, respuesta.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def pendientes(limite: int) -> list[dict[str, str]]:
    """FAQs que siguen en inglés, más antiguas primero.

    Se traen también ``intencion``, ``categoria``, ``fuente`` y ``licencia`` aunque no se
    traduzcan: el guardado es un *upsert*, es decir un INSERT que debe satisfacer las
    columnas ``NOT NULL``. Sin arrastrarlas, PostgREST responde 23502 y la traducción se
    pierde después de haberla pagado.
    """
    estado, cuerpo = _rest(
        "faq_externa?traducida=is.false"
        "&select=faq_id,pregunta,respuesta,intencion,categoria,fuente,licencia"
        f"&limit={limite}&order=faq_id"
    )
    if estado >= 400:
        raise SystemExit(f"no se pudieron leer las FAQs: {estado} {cuerpo}")
    return json.loads(cuerpo)


def traducir_lote(proveedor: Any, lote: list[dict[str, str]]) -> list[dict[str, str]]:
    """Pide la traducción del lote y devuelve las filas traducidas."""
    payload = json.dumps(
        [{"faq_id": f["faq_id"], "pregunta": f["pregunta"], "respuesta": f["respuesta"]} for f in lote],
        ensure_ascii=False,
    )
    respuesta = proveedor.completar(INSTRUCCION + payload, ESQUEMA, timeout_s=120.0)
    return respuesta.get("traducciones", [])


def main() -> int:
    parser = argparse.ArgumentParser(description="Traduce las FAQs externas a español peruano.")
    parser.add_argument("--limite", type=int, default=1000, help="máximo de FAQs a traducir")
    args = parser.parse_args()

    _cargar_env()
    from packages.llm_layer.providers import obtener_proveedor

    proveedor = obtener_proveedor()
    print(f"  proveedor: {proveedor.nombre}")

    total = 0
    while total < args.limite:
        lote = pendientes(min(POR_LOTE, args.limite - total))
        if not lote:
            break
        try:
            traducidas = traducir_lote(proveedor, lote)
        except Exception as exc:
            print(f"  fallo del proveedor: {type(exc).__name__}: {str(exc)[:140]}")
            break
        if not traducidas:
            print("  el proveedor no devolvió traducciones; se detiene")
            break
        por_id = {f["faq_id"]: f for f in lote}
        filas = [
            {
                **{k: por_id[t["faq_id"]][k] for k in ("intencion", "categoria", "fuente", "licencia")},
                "faq_id": t["faq_id"],
                "pregunta": t["pregunta"],
                "respuesta": t["respuesta"],
                "idioma": "es",
                "traducida": True,
            }
            for t in traducidas
            # Se descarta lo que el modelo devuelva con un faq_id que no se le pidió: es
            # la única forma de que una alucinación de identificador no cree filas nuevas.
            if t.get("faq_id") in por_id and t.get("pregunta") and t.get("respuesta")
        ]
        estado, cuerpo = _rest(
            "faq_externa?on_conflict=faq_id",
            metodo="POST",
            cuerpo=filas,
            extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if estado >= 400:
            print(f"  error guardando: {estado} {cuerpo[:200]}")
            return 1
        total += len(filas)
        print(f"    traducidas {total}...", flush=True)

    print(f"\n  total traducidas: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
