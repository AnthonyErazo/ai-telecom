export type Block =
  | { tipo: "texto"; titulo?: string | null; texto: string; enfasis?: boolean }
  | { tipo: "kv"; titulo?: string | null; items: Array<{ clave: string; valor: string; fact_id?: string | null }> }
  | { tipo: "puente"; titulo?: string | null; barras: Array<{ etiqueta: string; monto_cent: number; tipo: string; fact_id?: string | null }> }
  | { tipo: "tabla"; titulo?: string | null; columnas: string[]; filas: string[][]; nota?: string | null }
  | { tipo: "aviso"; titulo?: string | null; texto: string; severidad: string };

export interface FactSet {
  periodo_actual: string;
  total_previo_cent: number;
  total_actual_cent: number;
  delta_total_cent: number;
  modalidad_renta: string;
  deuda_anterior_cent?: number;
  fecha_vencimiento?: string;
  sha256: string;
  [key: string]: unknown;
}

export interface Explanation {
  conversation_id: string;
  trace_id: string;
  bloques: Block[];
  acciones: Array<{ id: string; etiqueta: string; riesgo: string }>;
  derivacion: { requerida: boolean; motivo?: string | null; resumen_asesor?: string | null };
  gobernanza: {
    anclado: boolean;
    verificacion_numerica: "PASS" | "FAIL" | "NO_APLICA";
    aserciones_totales: number;
    aserciones_ancladas: number;
    aserciones_no_ancladas: number;
    confianza: number;
    modo: string;
    model_version: string;
    rules_version: string;
    factset_sha256: string;
  };
  telemetria: Record<string, unknown>;
}

export interface DemoAccounts {
  demo: string[];
  guion: Record<string, string>;
}
