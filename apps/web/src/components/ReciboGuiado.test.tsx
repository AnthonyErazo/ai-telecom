import { describe, expect, it } from "vitest";
import { pasosDeExplicacion } from "./ReciboGuiado";
import type { Block } from "../api/types";

/**
 * Lo que se prueba aquí es la regla que hace honesta a la pantalla: **se señala lo que
 * dice el ancla, nunca lo que parece decir el texto**.
 *
 * Señalar la línea equivocada sería una mentira nueva, y de un tipo que el verificador
 * numérico no caza: no hay ninguna cifra inventada, simplemente el dedo apunta a otro
 * sitio. La única defensa es no interpretar el texto en ningún momento.
 */
describe("pasosDeExplicacion", () => {
  it("empareja cada frase con la línea que su ancla declara", () => {
    const bloques: Block[] = [
      { tipo: "texto", texto: "Le subió S/ 12,30.", fact_ids: ["factset:total_actual_cent"] },
      { tipo: "texto", texto: "Se le cobró la reactivación.", fact_ids: ["linea:CARGO_RECONEXION.delta_cent"] },
      { tipo: "texto", texto: "Bajó el paquete de datos.", fact_ids: ["linea:PAQUETE_DATOS.delta_cent"] },
    ];
    const pasos = pasosDeExplicacion(bloques);
    expect(pasos.map((p) => p.conceptos)).toEqual([[], ["CARGO_RECONEXION"], ["PAQUETE_DATOS"]]);
    expect(pasos[0].total).toBe(true);
  });

  it("un bloque sin anclas se lee pero no señala nada", () => {
    // Preferible a señalar a bulto: si el motor no dijo de qué línea habla, la pantalla
    // no se lo inventa. El cliente lee la frase con el recibo entero a la vista.
    const pasos = pasosDeExplicacion([{ tipo: "texto", texto: "Su plan incluye Movistar TV." }]);
    expect(pasos).toHaveLength(1);
    expect(pasos[0].conceptos).toEqual([]);
    expect(pasos[0].total).toBe(false);
  });

  it("ignora los bloques que no son narrativos", () => {
    // La tabla y el kv ya muestran sus propias cifras en el recibo; convertirlos en pasos
    // haría que la explicación se leyera dos veces.
    const bloques: Block[] = [
      { tipo: "kv", items: [{ clave: "Total", valor: "S/ 60,00" }] },
      { tipo: "tabla", columnas: ["a"], filas: [["1"]] },
      { tipo: "aviso", texto: "Tiene un saldo pendiente.", severidad: "info", fact_ids: [] },
    ];
    expect(pasosDeExplicacion(bloques).map((p) => p.texto)).toEqual(["Tiene un saldo pendiente."]);
  });

  it("descarta los bloques vacíos", () => {
    expect(pasosDeExplicacion([{ tipo: "texto", texto: "   " }])).toEqual([]);
  });

  it("acepta varias líneas en un mismo paso", () => {
    const pasos = pasosDeExplicacion([
      {
        tipo: "texto",
        texto: "Dos conceptos se movieron.",
        fact_ids: ["linea:A.delta_cent", "linea:B.delta_cent"],
      },
    ]);
    expect(pasos[0].conceptos).toEqual(["A", "B"]);
  });
});
