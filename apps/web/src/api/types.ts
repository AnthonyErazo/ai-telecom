export type Block =
  | { tipo: "texto"; titulo?: string | null; texto: string; enfasis?: boolean; fact_ids?: string[] }
  | { tipo: "kv"; titulo?: string | null; items: Array<{ clave: string; valor: string; fact_id?: string | null }> }
  | { tipo: "puente"; titulo?: string | null; barras: Array<{ etiqueta: string; monto_cent: number; tipo: string; fact_id?: string | null }> }
  | { tipo: "tabla"; titulo?: string | null; columnas: string[]; filas: string[][]; nota?: string | null }
  | { tipo: "aviso"; titulo?: string | null; texto: string; severidad: string; fact_ids?: string[] };

/** Fracción homogénea del ciclo: misma tarifa y mismo estado de servicio.
 *
 * Es la explicación del prorrateo tal como la calcula el backend (`Tramo` en
 * `packages/core_domain/esquemas/recibo.py`): "del 1 al 12 de julio el Plan A, del
 * 13 al 30 el Plan B". Ya viaja dentro de `GET /v1/hechos`, solo faltaba tipar. */
export interface Tramo {
  inicio: string;
  fin: string;
  dias: number;
  tarifa_mensual_cent: number;
  estado: "ACTIVO" | "SUSPENDIDO";
  facturable: boolean;
  monto_prorrateado_cent: number;
  etiqueta: string;
  concepto_id?: string | null;
  plan?: string | null;
  descuento_cent?: number;
}

/** Agrupación de deltas por causa, en el vocabulario del cliente (backend: `CausaAgregada`). */
export interface CausaAgregada {
  causa?: string | null;
  causa_oficial?: string | null;
  etiqueta_cliente: string;
  monto_cent: number;
  participacion_bp: number;
}

/** Una cuota del cronograma francés de un equipo financiado. */
export interface CuotaFinanciamiento {
  numero: number;
  de_total: number;
  monto_cent: number;
  saldo_final_cent: number;
}

/** Cronograma completo de un equipo financiado (backend: `PlanFinanciamiento`). */
export interface PlanFinanciamiento {
  equipo: string;
  principal_cent: number;
  cuotas_totales: number;
  cronograma: CuotaFinanciamiento[];
}

/** Una línea del recibo, tal como la devuelve `GET /v1/hechos`. */
export interface LineaFactSet {
  concepto_id: string;
  nombre_comercial: string;
  clase: string;
  monto_actual_cent: number;
  monto_previo_cent: number;
  delta_cent: number;
  causa?: string | null;
  familia?: string | null;
  causa_oficial?: string | null;
  dias_prorrateo?: number | null;
  tramos?: Tramo[] | null;
  cuota_numero?: number | null;
  cuotas_totales?: number | null;
  confianza?: number;
}

/** Una fila de `GET /v1/historial`: resumen de un periodo, sin el detalle línea a línea. */
export interface ResumenRecibo {
  periodo: string;
  total_cent: number;
  fecha_emision: string;
  fecha_vencimiento: string;
  modalidad_renta: string;
  deuda_anterior_cent: number;
  estado_servicio: string;
  es_actual: boolean;
}

export interface FactSet {
  lineas?: LineaFactSet[];
  periodo_actual: string;
  total_previo_cent: number;
  total_actual_cent: number;
  delta_total_cent: number;
  modalidad_renta: "ADELANTADA" | "VENCIDA" | string;
  dias_ciclo?: number;
  periodo_previo?: string;
  deuda_anterior_cent?: number;
  fecha_vencimiento?: string;
  ciclo_inicio?: string | null;
  ciclo_fin?: string | null;
  causas_agregadas?: CausaAgregada[];
  beneficios_vigentes?: string[];
  financiamientos?: PlanFinanciamiento[];
  total_a_pagar_cent?: number;
  sha256: string;
  [key: string]: unknown;
}

export interface Explanation {
  conversation_id: string;
  trace_id: string;
  bloques: Block[];
  acciones: Array<{ id: string; etiqueta: string; riesgo: string; payload?: Record<string, unknown> }>;
  derivacion: {
    requerida: boolean;
    motivo?: string | null;
    resumen_asesor?: string | null;
    // La referencia con la que el asesor recupera el expediente. Sin ella, la consola no
    // puede abrir el caso que el cliente acaba de derivar.
    context_ref?: string | null;
    motivo_codigo?: string | null;
  };
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

// --------------------------------------------------------------------------- //
// Consola del asesor (canal ASESOR)
// --------------------------------------------------------------------------- //
/** Un caso derivado esperando a que alguien lo recoja. */
export interface ElementoCola {
  context_ref: string;
  conversation_id: string;
  cuenta_id?: string | null;
  motivo_codigo?: string | null;
  resumen_asesor?: string | null;
  trace_id?: string | null;
  creado_en?: string | null;
}

export interface TurnoSala {
  rol: string;
  texto: string;
  ts?: string | null;
  autor?: string | null;
}

/** Estado de la sala compartida: quién atiende y qué se ha dicho. */
export interface EstadoSala {
  conversation_id: string;
  modo: string;
  asesor?: string | null;
  derivada: boolean;
  turnos: TurnoSala[];
  resumen_asesor?: string | null;
  cuenta_id?: string | null;
}

export interface LineaPaquete {
  concepto_id: string;
  nombre_comercial: string;
  clase: string;
  monto_previo_cent: number;
  monto_actual_cent: number;
  delta_cent: number;
  causa?: string | null;
  confianza: number;
  atribuida: boolean;
}

/** Lo que el sistema NO pudo confirmar. Es el campo que hace responsable el traspaso. */
export interface Incertidumbre {
  codigo: string;
  detalle: string;
  impacto_cent?: number | null;
  evidencia: string[];
}

/**
 * El expediente que recibe el asesor, reconstruido desde la bitácora encadenada.
 *
 * No es un resumen bonito: cada campo tiene respaldo en un evento sellado por hash, y
 * `evidencia.consulta_auditoria` dice cómo comprobarlo.
 */
export interface PaqueteAsesor {
  context_ref?: string | null;
  conversation_id?: string | null;
  cuenta_id?: string | null;
  canal: string;
  motivo_codigo?: string | null;
  motivo_detalle?: string | null;
  accion_pendiente: string;
  consulta_cliente: string;
  periodo_actual?: string | null;
  periodo_previo?: string | null;
  total_previo_cent?: number | null;
  total_actual_cent?: number | null;
  delta_total_cent?: number | null;
  deuda_anterior_cent?: number | null;
  fecha_vencimiento?: string | null;
  modalidad_renta?: string | null;
  lineas: LineaPaquete[];
  causas: Array<{ etiqueta_cliente: string; monto_cent: number; participacion_bp: number; confianza: number }>;
  residual_cent?: number | null;
  invariante_ok: boolean;
  ya_explicado: {
    hubo_explicacion: boolean;
    texto?: string | null;
    modo?: string | null;
    veredicto?: string | null;
    cifras: Array<{ texto: string; token: string; estado: string; fuente?: string | null }>;
  };
  incertidumbres: Incertidumbre[];
  brief?: string | null;
  verificacion_brief?: {
    veredicto: string;
    aserciones_totales: number;
    ancladas: number;
    no_ancladas: string[];
  } | null;
  evidencia: {
    trace_id: string;
    eventos: number;
    cadena_valida: boolean;
    consulta_auditoria: string;
    factset_sha256?: string | null;
  };
}

export interface LiveToken {
  token: string;
  model: string;
  voice: string;
  expire_time: string;
  new_session_expire_time: string;
}
