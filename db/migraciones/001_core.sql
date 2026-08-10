-- =============================================================================
-- 001_core.sql — modelo transaccional de recibo-claro
-- -----------------------------------------------------------------------------
-- Espejo en PostgreSQL 16 de packages/core_domain/esquemas/. Reglas que gobiernan
-- este fichero:
--
--   * TODO importe es BIGINT en CÉNTIMOS. No hay NUMERIC ni DOUBLE en ninguna
--     columna monetaria: el redondeo se decide en Python (reparto por mayor resto)
--     y la base solo guarda enteros exactos.
--   * Los rangos de fechas son [inicio, fin) con el extremo derecho EXCLUSIVO,
--     igual que `Tramo` y `Recibo.ciclo_fin`.
--   * Los CHECK no son decoración: reproducen los `model_validator` de Pydantic.
--     Lo que la aplicación no deja construir, la base no lo deja almacenar.
--   * Los identificadores de cliente y cuenta son TOKENIZADOS. Hay un CHECK que
--     rechaza los formatos de DNI (8 dígitos) y de móvil peruano (9 dígitos que
--     empiezan por 9): la ficha exige datos sin DNI ni teléfono, y aquí se cumple
--     aunque el cargador se equivoque.
--
-- Los tipos ENUM replican literalmente packages/core_domain/enums.py. Si allí se
-- añade un valor, aquí hay que emitir un ALTER TYPE en una migración nueva y subir
-- `rules_version`: los valores forman parte del contrato.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Tipos enumerados (espejo de core_domain/enums.py)
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    CREATE TYPE modalidad_renta AS ENUM ('ADELANTADA', 'VENCIDA');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE tipo_movimiento AS ENUM (
        'CAMBIO_PLAN', 'SUSPENSION', 'RECONEXION', 'ALTA_SERVICIO', 'BAJA_SERVICIO',
        'FIN_DESCUENTO', 'ALTA_PAQUETE', 'ALTA_EQUIPO_FINANCIADO', 'NOTA_CREDITO',
        'NOTA_DEBITO', 'AJUSTE_SUSPENSION');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE clase_delta AS ENUM ('NUEVO', 'DESAPARECIDO', 'SUBIO', 'BAJO', 'IGUAL');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE familia_concepto AS ENUM (
        'RECURRENTE', 'UNICO', 'AJUSTE', 'FINANCIAMIENTO', 'IMPUESTO', 'CREDITO');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE estado_servicio AS ENUM ('ACTIVO', 'SUSPENDIDO');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE nivel_aseguramiento AS ENUM ('LOA0', 'LOA1', 'LOA2', 'LOA_ASESOR');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE verbosidad AS ENUM ('CORTO', 'DETALLE');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE modo_generacion AS ENUM ('LLM', 'LLM_REINTENTO', 'PLANTILLA');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE accion_siguiente AS ENUM (
        'PAGAR', 'VER_DETALLE', 'REGISTRAR_CONSULTA', 'VER_ALTERNATIVAS', 'DERIVAR_ASESOR');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    -- Las 9 causas oficiales, literal de la ficha del Desafío 1.
    CREATE TYPE causa_oficial AS ENUM (
        'CAMBIO_DE_PLAN', 'EQUIPO_FINANCIADO', 'COMPRA_DE_PAQUETES', 'CARGOS_ADICIONALES',
        'PROMOCIONES_VENCIDAS', 'NOTAS_CREDITO_DEBITO', 'PRORRATEOS', 'RECONEXIONES',
        'AJUSTES_POR_DIAS_DE_SUSPENSION');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE canal AS ENUM ('APP', 'BOT', 'WHATSAPP', 'ASESOR');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE veredicto_verificacion AS ENUM ('PASS', 'FAIL', 'NO_APLICA');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE motivo_derivacion AS ENUM (
        'PETICION_HUMANO', 'INVARIANTE_ROTO', 'CONCEPTO_FUERA_CATALOGO',
        'INTENCION_REGULATORIA', 'UMBRAL_INCOMPRENSION', 'VERIFICACION_FALLIDA',
        'NIVEL_INSUFICIENTE');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE tipo_evidencia AS ENUM (
        'linea', 'mov', 'cat', 'tramo', 'faq', 'casuistica', 'regla', 'factset');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE TYPE rol_turno AS ENUM ('CLIENTE', 'ASISTENTE', 'SISTEMA');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- -----------------------------------------------------------------------------
-- Función común: rechaza identificadores con pinta de PII real
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION es_referencia_tokenizada(p_ref text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
AS $$
    -- Falso si parece un DNI peruano (8 dígitos) o un móvil (9 dígitos que empiezan
    -- por 9). La ficha exige dummy data "sin DNI ni teléfono"; esto lo hace estructural.
    SELECT p_ref IS NOT NULL
       AND p_ref !~ '^[0-9]{8}$'
       AND p_ref !~ '^9[0-9]{8}$';
$$;

COMMENT ON FUNCTION es_referencia_tokenizada(text) IS
    'Rechaza identificadores con forma de DNI (8 dígitos) o de móvil peruano (9 dígitos, prefijo 9).';


-- -----------------------------------------------------------------------------
-- cliente
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cliente (
    cliente_id      text PRIMARY KEY,
    segmento        text        NOT NULL DEFAULT 'MASIVO',
    antiguedad_meses integer    NOT NULL DEFAULT 0,
    creado_en       timestamptz NOT NULL DEFAULT now(),
    meta            jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_cliente_tokenizado  CHECK (es_referencia_tokenizada(cliente_id)),
    CONSTRAINT ck_cliente_antiguedad  CHECK (antiguedad_meses >= 0)
);

COMMENT ON TABLE  cliente             IS 'Cliente B2C ficticio. Nunca contiene PII real.';
COMMENT ON COLUMN cliente.cliente_id  IS 'Referencia tokenizada; jamás DNI, teléfono ni correo.';
COMMENT ON COLUMN cliente.segmento    IS 'Segmento comercial simulado (MASIVO, PREMIUM, ...).';


-- -----------------------------------------------------------------------------
-- cuenta
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cuenta (
    cuenta_id        text PRIMARY KEY,
    cliente_id       text NOT NULL REFERENCES cliente (cliente_id) ON DELETE CASCADE,
    modalidad_renta  modalidad_renta NOT NULL,
    dia_ciclo        smallint        NOT NULL,
    plan_vigente     text,
    tarifa_plan_cent bigint          NOT NULL DEFAULT 0,
    estado_servicio  estado_servicio NOT NULL DEFAULT 'ACTIVO',
    creado_en        timestamptz     NOT NULL DEFAULT now(),
    meta             jsonb           NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_cuenta_tokenizada CHECK (es_referencia_tokenizada(cuenta_id)),
    -- Día 29-31 no existe en todos los meses: el generador nunca los usa.
    CONSTRAINT ck_cuenta_dia_ciclo  CHECK (dia_ciclo BETWEEN 1 AND 28),
    CONSTRAINT ck_cuenta_tarifa     CHECK (tarifa_plan_cent >= 0)
);

CREATE INDEX IF NOT EXISTS ix_cuenta_cliente ON cuenta (cliente_id);

COMMENT ON TABLE  cuenta                  IS 'Cuenta facturable. La clave de todo el dominio es (cuenta_id, periodo).';
COMMENT ON COLUMN cuenta.modalidad_renta  IS 'ADELANTADA cobra la renta del ciclo siguiente y corrige el actual; VENCIDA cobra el ciclo cerrado.';
COMMENT ON COLUMN cuenta.dia_ciclo        IS 'Día del mes en que abre el ciclo de facturación (1..28).';
COMMENT ON COLUMN cuenta.tarifa_plan_cent IS 'Tarifa mensual vigente en CÉNTIMOS enteros.';


-- -----------------------------------------------------------------------------
-- recibo
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recibo (
    recibo_id           text PRIMARY KEY,
    cuenta_id           text            NOT NULL REFERENCES cuenta (cuenta_id) ON DELETE CASCADE,
    periodo             char(7)         NOT NULL,
    modalidad_renta     modalidad_renta NOT NULL,
    ciclo_inicio        date            NOT NULL,
    ciclo_fin           date            NOT NULL,
    dias_ciclo          integer         NOT NULL,
    fecha_emision       date            NOT NULL,
    fecha_vencimiento   date            NOT NULL,
    total_cent          bigint          NOT NULL,
    deuda_anterior_cent bigint          NOT NULL DEFAULT 0,
    moneda              char(3)         NOT NULL DEFAULT 'PEN',
    estado_servicio     estado_servicio NOT NULL DEFAULT 'ACTIVO',
    plan_vigente        text,
    escenario           text,
    creado_en           timestamptz     NOT NULL DEFAULT now(),
    meta                jsonb           NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT uq_recibo_cuenta_periodo UNIQUE (cuenta_id, periodo),
    CONSTRAINT ck_recibo_periodo    CHECK (periodo ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    CONSTRAINT ck_recibo_ciclo      CHECK (ciclo_fin > ciclo_inicio),
    -- Espejo exacto de Recibo._validar_conciliacion: dias_ciclo = (ciclo_fin - ciclo_inicio).
    CONSTRAINT ck_recibo_dias       CHECK (dias_ciclo = (ciclo_fin - ciclo_inicio)),
    CONSTRAINT ck_recibo_vencimiento CHECK (fecha_vencimiento >= fecha_emision),
    CONSTRAINT ck_recibo_deuda      CHECK (deuda_anterior_cent >= 0),
    CONSTRAINT ck_recibo_moneda     CHECK (moneda = 'PEN')
);

CREATE INDEX IF NOT EXISTS ix_recibo_cuenta_periodo ON recibo (cuenta_id, periodo DESC);
CREATE INDEX IF NOT EXISTS ix_recibo_periodo        ON recibo (periodo);
CREATE INDEX IF NOT EXISTS ix_recibo_escenario      ON recibo (escenario) WHERE escenario IS NOT NULL;

COMMENT ON TABLE  recibo                     IS 'Recibo de un periodo tal como lo expone BrainyBill (actual + 5 previos).';
COMMENT ON COLUMN recibo.periodo             IS 'Periodo de facturación en formato YYYY-MM.';
COMMENT ON COLUMN recibo.ciclo_fin           IS 'Extremo derecho EXCLUSIVO del ciclo: el rango es [ciclo_inicio, ciclo_fin).';
COMMENT ON COLUMN recibo.dias_ciclo          IS 'D del prorrateo. Se valida contra el rango: no puede mentir.';
COMMENT ON COLUMN recibo.total_cent          IS 'Total del periodo en CÉNTIMOS. Debe igualar la suma de recibo_linea (trigger diferido).';
COMMENT ON COLUMN recibo.deuda_anterior_cent IS 'Deuda arrastrada. NO forma parte de total_cent: no contamina el delta entre recibos.';
COMMENT ON COLUMN recibo.escenario           IS 'Escenario sintético inyectado por datagen (CAMBIO_PLAN_MEDIO_CICLO, ESTABLE, ...).';


-- -----------------------------------------------------------------------------
-- recibo_linea
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recibo_linea (
    linea_id        bigserial PRIMARY KEY,
    recibo_id       text             NOT NULL REFERENCES recibo (recibo_id) ON DELETE CASCADE,
    cuenta_id       text             NOT NULL REFERENCES cuenta (cuenta_id) ON DELETE CASCADE,
    periodo         char(7)          NOT NULL,
    concepto_id     text             NOT NULL,
    nombre_comercial text            NOT NULL,
    familia         familia_concepto NOT NULL,
    monto_cent      bigint           NOT NULL,
    servicio_id     text,
    descripcion     text,
    cantidad        integer          NOT NULL DEFAULT 1,
    afecto_igv      boolean          NOT NULL DEFAULT true,
    dias_prorrateo  integer,
    fecha_inicio    date,
    fecha_fin       date,
    cuota_numero    integer,
    cuotas_totales  integer,
    movimiento_id   bigint,
    tramos          jsonb            NOT NULL DEFAULT '[]'::jsonb,
    meta            jsonb            NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_linea_cantidad CHECK (cantidad > 0),
    CONSTRAINT ck_linea_dias     CHECK (dias_prorrateo IS NULL OR dias_prorrateo >= 0),
    -- Rango [inicio, fin) con fin exclusivo, igual que en Tramo.
    CONSTRAINT ck_linea_rango    CHECK (fecha_inicio IS NULL OR fecha_fin IS NULL OR fecha_fin > fecha_inicio),
    -- Espejo de LineaRecibo._validar_cuotas: "cuota 3 de 18" nunca puede ser "cuota 19 de 18".
    CONSTRAINT ck_linea_cuota    CHECK (
        (cuota_numero IS NULL AND cuotas_totales IS NULL)
        OR (cuota_numero IS NOT NULL AND cuotas_totales IS NOT NULL
            AND cuota_numero BETWEEN 1 AND cuotas_totales)
    ),
    CONSTRAINT ck_linea_tramos   CHECK (jsonb_typeof(tramos) = 'array'),
    -- Un concepto de familia CREDITO resta por definición (ConceptoCatalogo.es_credito).
    -- El IGV NO lleva CHECK de signo: sobre una base afecta negativa el impuesto también
    -- es negativo, y ese caso existe (mes dominado por una nota de crédito).
    CONSTRAINT ck_linea_credito  CHECK (familia <> 'CREDITO' OR monto_cent <= 0)
);

CREATE INDEX IF NOT EXISTS ix_linea_recibo    ON recibo_linea (recibo_id);
CREATE INDEX IF NOT EXISTS ix_linea_concepto  ON recibo_linea (cuenta_id, periodo, concepto_id);
CREATE INDEX IF NOT EXISTS ix_linea_movimiento ON recibo_linea (movimiento_id) WHERE movimiento_id IS NOT NULL;

COMMENT ON TABLE  recibo_linea             IS 'Detalle del recibo. El IGV es UNA LÍNEA más (familia IMPUESTO), no un campo aparte.';
COMMENT ON COLUMN recibo_linea.concepto_id IS 'Clave del catálogo. SIN clave foránea a propósito: un concepto desconocido debe poder ingerirse para que el motor lo detecte y DERIVE, en vez de romper la carga.';
COMMENT ON COLUMN recibo_linea.monto_cent  IS 'Importe en CÉNTIMOS con signo (negativo en créditos y ajustes a favor).';
COMMENT ON COLUMN recibo_linea.tramos      IS 'Tramos del prorrateo serializados: la tabla de tramos ES la explicación.';
COMMENT ON COLUMN recibo_linea.fecha_fin   IS 'EXCLUSIVA. El último día cubierto es fecha_fin - 1.';


-- Invariante Σ líneas == total_cent. No cabe en un CHECK (es entre filas), así que va
-- en un trigger de restricción DIFERIDO: la carga puede insertar el recibo y luego sus
-- líneas dentro de la misma transacción, y el descuadre estalla al hacer COMMIT.
CREATE OR REPLACE FUNCTION fn_recibo_concilia()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_recibo_id text;
    v_suma      bigint;
    v_total     bigint;
BEGIN
    v_recibo_id := COALESCE(NEW.recibo_id, OLD.recibo_id);

    SELECT total_cent INTO v_total FROM recibo WHERE recibo_id = v_recibo_id;
    IF NOT FOUND THEN
        RETURN NULL;  -- el recibo se borró en esta misma transacción
    END IF;

    SELECT COALESCE(sum(monto_cent), 0) INTO v_suma
      FROM recibo_linea WHERE recibo_id = v_recibo_id;

    IF v_suma <> v_total THEN
        RAISE EXCEPTION
            'recibo % no concilia: las líneas suman % céntimos y total_cent es % (descuadre de %)',
            v_recibo_id, v_suma, v_total, v_suma - v_total
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;

COMMENT ON FUNCTION fn_recibo_concilia() IS
    'Invariante estructural: la suma de recibo_linea debe igualar recibo.total_cent al cerrar la transacción.';

DROP TRIGGER IF EXISTS tg_recibo_linea_concilia ON recibo_linea;
CREATE CONSTRAINT TRIGGER tg_recibo_linea_concilia
    AFTER INSERT OR UPDATE OR DELETE ON recibo_linea
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION fn_recibo_concilia();


-- -----------------------------------------------------------------------------
-- movimiento (historial de órdenes estilo Amdocs)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS movimiento (
    movimiento_id bigint PRIMARY KEY,
    cuenta_id     text            NOT NULL REFERENCES cuenta (cuenta_id) ON DELETE CASCADE,
    tipo          tipo_movimiento NOT NULL,
    ocurrido_en   timestamptz     NOT NULL,
    detalle       jsonb           NOT NULL DEFAULT '{}'::jsonb,
    canal         canal,
    servicio_id   text,
    origen        text            NOT NULL DEFAULT 'amdocs',
    creado_en     timestamptz     NOT NULL DEFAULT now(),

    CONSTRAINT ck_movimiento_detalle CHECK (jsonb_typeof(detalle) = 'object')
);

-- La atribución de causa recorre los movimientos del ciclo ordenados por fecha:
-- este índice es el que sostiene esa ventana.
CREATE INDEX IF NOT EXISTS ix_movimiento_cuenta_fecha ON movimiento (cuenta_id, ocurrido_en);
CREATE INDEX IF NOT EXISTS ix_movimiento_tipo         ON movimiento (tipo, ocurrido_en);
CREATE INDEX IF NOT EXISTS ix_movimiento_detalle_gin  ON movimiento USING gin (detalle jsonb_path_ops);

COMMENT ON TABLE  movimiento            IS 'Evento del historial de órdenes capaz de explicar una variación (MovementEvent).';
COMMENT ON COLUMN movimiento.detalle    IS 'Payload tipado por tipo de movimiento: DetalleCambioPlan, DetalleReconexion, etc.';
COMMENT ON COLUMN movimiento.ocurrido_en IS 'Instante del evento. La distancia temporal a la línea decide la causa cuando hay varios candidatos.';


-- -----------------------------------------------------------------------------
-- pago
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pago (
    pago_id     bigserial PRIMARY KEY,
    cuenta_id   text        NOT NULL REFERENCES cuenta (cuenta_id) ON DELETE CASCADE,
    recibo_id   text        REFERENCES recibo (recibo_id) ON DELETE SET NULL,
    periodo     char(7),
    monto_cent  bigint      NOT NULL,
    fecha_pago  date        NOT NULL,
    medio       text        NOT NULL DEFAULT 'DESCONOCIDO',
    referencia  text,
    creado_en   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_pago_monto   CHECK (monto_cent > 0),
    CONSTRAINT ck_pago_periodo CHECK (periodo IS NULL OR periodo ~ '^[0-9]{4}-(0[1-9]|1[0-2])$')
);

CREATE INDEX IF NOT EXISTS ix_pago_cuenta_fecha ON pago (cuenta_id, fecha_pago DESC);

COMMENT ON TABLE  pago            IS 'Pago aplicado. Alimenta la deuda anterior y la sugerencia de acción PAGAR.';
COMMENT ON COLUMN pago.monto_cent IS 'Importe pagado en CÉNTIMOS, siempre positivo.';


-- -----------------------------------------------------------------------------
-- deuda_snapshot
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS deuda_snapshot (
    cuenta_id       text        NOT NULL REFERENCES cuenta (cuenta_id) ON DELETE CASCADE,
    periodo         char(7)     NOT NULL,
    fecha_corte     date        NOT NULL,
    saldo_cent      bigint      NOT NULL,
    vencido_cent    bigint      NOT NULL DEFAULT 0,
    dias_mora       integer     NOT NULL DEFAULT 0,
    estado_servicio estado_servicio NOT NULL DEFAULT 'ACTIVO',
    creado_en       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (cuenta_id, periodo),
    CONSTRAINT ck_deuda_periodo CHECK (periodo ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    CONSTRAINT ck_deuda_saldo   CHECK (saldo_cent >= 0 AND vencido_cent >= 0),
    CONSTRAINT ck_deuda_vencido CHECK (vencido_cent <= saldo_cent),
    CONSTRAINT ck_deuda_mora    CHECK (dias_mora >= 0)
);

COMMENT ON TABLE  deuda_snapshot            IS 'Foto de la deuda al cierre de cada periodo. Explica la suspensión por morosidad y el cargo de reconexión.';
COMMENT ON COLUMN deuda_snapshot.saldo_cent IS 'Saldo total en CÉNTIMOS al corte.';
COMMENT ON COLUMN deuda_snapshot.dias_mora  IS 'Días de atraso; cruzado con politica.dias_gracia_suspension de rules.yaml.';


-- -----------------------------------------------------------------------------
-- conversacion / turno
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversacion (
    conversation_id uuid PRIMARY KEY,
    cuenta_id       text        REFERENCES cuenta (cuenta_id) ON DELETE SET NULL,
    canal           canal       NOT NULL DEFAULT 'APP',
    nivel           nivel_aseguramiento NOT NULL DEFAULT 'LOA2',
    abierta_en      timestamptz NOT NULL DEFAULT now(),
    cerrada_en      timestamptz,
    derivada        boolean     NOT NULL DEFAULT false,
    context_ref     text,
    meta            jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_conversacion_cierre CHECK (cerrada_en IS NULL OR cerrada_en >= abierta_en),
    -- Histéresis del hand-off: si se derivó, tiene que quedar la referencia del contexto.
    CONSTRAINT ck_conversacion_contexto CHECK (NOT derivada OR context_ref IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_conversacion_cuenta ON conversacion (cuenta_id, abierta_en DESC);

COMMENT ON TABLE  conversacion             IS 'Sesión conversacional en App, Bot Lucía o WhatsApp.';
COMMENT ON COLUMN conversacion.nivel       IS 'LOA con el que se atendió: LOA1 (WhatsApp) no puede ver montos.';
COMMENT ON COLUMN conversacion.context_ref IS 'Referencia del contexto transferido al asesor en el hand-off.';

CREATE TABLE IF NOT EXISTS turno (
    turno_id        bigserial PRIMARY KEY,
    conversation_id uuid        NOT NULL REFERENCES conversacion (conversation_id) ON DELETE CASCADE,
    indice          integer     NOT NULL,
    rol             rol_turno   NOT NULL,
    texto           text        NOT NULL DEFAULT '',
    trace_id        text,
    verbosidad      verbosidad,
    ocurrido_en     timestamptz NOT NULL DEFAULT now(),
    meta            jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT ck_turno_indice CHECK (indice >= 0),
    CONSTRAINT uq_turno_conversacion_indice UNIQUE (conversation_id, indice)
);

CREATE INDEX IF NOT EXISTS ix_turno_conversacion ON turno (conversation_id, indice);
CREATE INDEX IF NOT EXISTS ix_turno_trace        ON turno (trace_id) WHERE trace_id IS NOT NULL;

COMMENT ON TABLE  turno          IS 'Turno de la conversación. La similitud entre turnos consecutivos alimenta s3 del score de incomprensión.';
COMMENT ON COLUMN turno.texto    IS 'Mensaje. El del cliente entra al prompt como DATO delimitado, nunca como instrucción.';
COMMENT ON COLUMN turno.trace_id IS 'Enlaza el turno con su cadena de auditoría (auditoria_evento.trace_id).';


-- -----------------------------------------------------------------------------
-- factset
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS factset (
    factset_id          uuid PRIMARY KEY,
    cuenta_id           text            NOT NULL REFERENCES cuenta (cuenta_id) ON DELETE CASCADE,
    periodo_actual      char(7)         NOT NULL,
    periodo_previo      char(7)         NOT NULL,
    modalidad_renta     modalidad_renta NOT NULL,
    dias_ciclo          integer         NOT NULL,
    total_actual_cent   bigint          NOT NULL,
    total_previo_cent   bigint          NOT NULL,
    delta_total_cent    bigint          NOT NULL,
    deuda_anterior_cent bigint          NOT NULL DEFAULT 0,
    invariante_ok       boolean         NOT NULL,
    residual_cent       bigint          NOT NULL,
    suma_deltas_cent    bigint          NOT NULL,
    confianza_global    real            NOT NULL,
    firma_causal        text,
    rules_version       text            NOT NULL,
    sha256              char(64)        NOT NULL,
    documento           jsonb           NOT NULL,
    generado_en         timestamptz     NOT NULL DEFAULT now(),

    CONSTRAINT uq_factset_cuenta_periodo UNIQUE (cuenta_id, periodo_actual, rules_version),
    CONSTRAINT ck_factset_periodos  CHECK (
        periodo_actual ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND periodo_previo ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
        AND periodo_previo < periodo_actual),
    CONSTRAINT ck_factset_dias      CHECK (dias_ciclo > 0),
    -- Espejo de FactSet._validar_totales.
    CONSTRAINT ck_factset_delta     CHECK (delta_total_cent = total_actual_cent - total_previo_cent),
    -- Espejo de Invariante.evaluar: la bandera NO puede mentir sobre el residual.
    CONSTRAINT ck_factset_invariante CHECK (invariante_ok = (abs(residual_cent) <= 1)),
    CONSTRAINT ck_factset_residual  CHECK (residual_cent = delta_total_cent - suma_deltas_cent),
    CONSTRAINT ck_factset_confianza CHECK (confianza_global BETWEEN 0 AND 1),
    CONSTRAINT ck_factset_sha       CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_factset_documento CHECK (jsonb_typeof(documento) = 'object')
);

CREATE INDEX IF NOT EXISTS ix_factset_cuenta   ON factset (cuenta_id, periodo_actual DESC);
CREATE INDEX IF NOT EXISTS ix_factset_sha      ON factset (sha256);
CREATE INDEX IF NOT EXISTS ix_factset_firma    ON factset (firma_causal) WHERE firma_causal IS NOT NULL;
-- Los FactSet rotos son el material de estudio del hand-off: se consultan aparte.
CREATE INDEX IF NOT EXISTS ix_factset_roto     ON factset (cuenta_id, periodo_actual) WHERE NOT invariante_ok;

COMMENT ON TABLE  factset                  IS 'Fotografía verificada de la variación entre dos recibos: el ÚNICO origen de cifras para el LLM.';
COMMENT ON COLUMN factset.documento        IS 'FactSet completo serializado (líneas, causas, tramos). Es lo que se hasheó y lo que vio el modelo.';
COMMENT ON COLUMN factset.sha256           IS 'SHA-256 del JSON canónico sin sha256 ni generado_en. Demuestra sobre qué hechos se redactó.';
COMMENT ON COLUMN factset.invariante_ok    IS 'Si es falso, la API responde 409 INVARIANTE_FALLIDO y deriva: no hay explicación aproximada.';
COMMENT ON COLUMN factset.residual_cent    IS 'delta_total - Σ deltas de línea, en CÉNTIMOS. Tolerancia ±1.';
COMMENT ON COLUMN factset.firma_causal     IS 'causas ordenadas + modalidad + signo(Δ). Recupera la casuística narrativa.';


-- -----------------------------------------------------------------------------
-- explicacion
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS explicacion (
    explicacion_id        uuid PRIMARY KEY,
    conversation_id       uuid    REFERENCES conversacion (conversation_id) ON DELETE SET NULL,
    turno_id              bigint  REFERENCES turno (turno_id) ON DELETE SET NULL,
    factset_id            uuid    REFERENCES factset (factset_id) ON DELETE SET NULL,
    cuenta_id             text    NOT NULL,  -- sin FK: ver COMMENT
    periodo               char(7) NOT NULL,
    trace_id              text    NOT NULL,
    canal                 canal   NOT NULL DEFAULT 'APP',
    nivel                 nivel_aseguramiento NOT NULL DEFAULT 'LOA2',
    verbosidad            verbosidad NOT NULL DEFAULT 'CORTO',
    texto                 text    NOT NULL DEFAULT '',
    bloques               jsonb   NOT NULL DEFAULT '[]'::jsonb,
    acciones              jsonb   NOT NULL DEFAULT '[]'::jsonb,
    -- gobernanza
    anclado               boolean NOT NULL,
    verificacion_numerica veredicto_verificacion NOT NULL,
    aserciones_totales    integer NOT NULL DEFAULT 0,
    aserciones_ancladas   integer NOT NULL DEFAULT 0,
    aserciones_derivadas  integer NOT NULL DEFAULT 0,
    aserciones_no_ancladas integer NOT NULL DEFAULT 0,
    confianza             real    NOT NULL DEFAULT 1.0,
    modo                  modo_generacion NOT NULL,
    rules_version         text    NOT NULL,
    model_version         text    NOT NULL,
    factset_sha256        char(64) NOT NULL,
    citas                 jsonb   NOT NULL DEFAULT '[]'::jsonb,
    latencia_ms           integer,
    -- derivación (hand-off)
    derivada              boolean NOT NULL DEFAULT false,
    motivo_derivacion     motivo_derivacion,
    context_ref           text,
    resumen_asesor        text,
    score_incomprension   real,
    -- telemetría
    silence_probe_id      uuid,
    creado_en             timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_explicacion_periodo   CHECK (periodo ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    CONSTRAINT ck_explicacion_conteos   CHECK (
        aserciones_totales >= 0 AND aserciones_ancladas >= 0
        AND aserciones_derivadas >= 0 AND aserciones_no_ancladas >= 0
        AND aserciones_ancladas + aserciones_derivadas + aserciones_no_ancladas
            <= aserciones_totales),
    -- La métrica comprometida (TA_respuesta = 0), impuesta por la base: es
    -- IMPOSIBLE almacenar un PASS que arrastre una aserción sin anclar.
    CONSTRAINT ck_explicacion_pass_limpio CHECK (
        verificacion_numerica <> 'PASS' OR aserciones_no_ancladas = 0),
    CONSTRAINT ck_explicacion_anclado   CHECK (anclado = (aserciones_no_ancladas = 0)),
    CONSTRAINT ck_explicacion_confianza CHECK (confianza BETWEEN 0 AND 1),
    CONSTRAINT ck_explicacion_score     CHECK (score_incomprension IS NULL
                                               OR score_incomprension BETWEEN 0 AND 1),
    CONSTRAINT ck_explicacion_sha       CHECK (factset_sha256 ~ '^[0-9a-f]{64}$'),
    -- Un hand-off sin contexto no es un hand-off: la ficha pide "derivar CON CONTEXTO".
    CONSTRAINT ck_explicacion_handoff   CHECK (
        NOT derivada OR (motivo_derivacion IS NOT NULL AND resumen_asesor IS NOT NULL)),
    CONSTRAINT ck_explicacion_latencia  CHECK (latencia_ms IS NULL OR latencia_ms >= 0)
);

CREATE INDEX IF NOT EXISTS ix_explicacion_cuenta   ON explicacion (cuenta_id, periodo DESC);
CREATE INDEX IF NOT EXISTS ix_explicacion_trace    ON explicacion (trace_id);
CREATE INDEX IF NOT EXISTS ix_explicacion_conv     ON explicacion (conversation_id, creado_en);
CREATE INDEX IF NOT EXISTS ix_explicacion_sonda    ON explicacion (silence_probe_id) WHERE silence_probe_id IS NOT NULL;
-- Las respuestas no limpias son el material de la métrica de alucinación: índice parcial.
CREATE INDEX IF NOT EXISTS ix_explicacion_no_pass  ON explicacion (creado_en DESC)
    WHERE verificacion_numerica <> 'PASS';

COMMENT ON TABLE  explicacion                       IS 'Respuesta entregada al cliente con toda su gobernanza. Una fila por explicación.';
COMMENT ON COLUMN explicacion.cuenta_id             IS 'Sin clave foránea a propósito: la explicación es un registro histórico y debe sobrevivir a la purga de la cuenta.';
COMMENT ON COLUMN explicacion.texto                 IS 'Superficie exacta que auditó el verificador (RespuestaCanalAgnostica.texto).';
COMMENT ON COLUMN explicacion.aserciones_no_ancladas IS 'Cifras del texto sin respaldo en el FactSet. Con verificacion PASS debe ser 0, y el CHECK lo obliga.';
COMMENT ON COLUMN explicacion.modo                  IS 'LLM, LLM_REINTENTO o PLANTILLA (fallback determinístico).';
COMMENT ON COLUMN explicacion.citas                 IS 'Spans [inicio, fin) del texto enlazados a su fact_id del FactSet.';
COMMENT ON COLUMN explicacion.silence_probe_id      IS 'Sonda de la tasa de silencio post-explicación (packages/governance/telemetria.py).';
COMMENT ON COLUMN explicacion.score_incomprension   IS 'U de la sección 4.8; por encima de tau_alto se deriva.';


-- -----------------------------------------------------------------------------
-- evidencia
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidencia (
    evidencia_id   bigserial PRIMARY KEY,
    explicacion_id uuid           NOT NULL REFERENCES explicacion (explicacion_id) ON DELETE CASCADE,
    orden          integer        NOT NULL DEFAULT 0,
    tipo           tipo_evidencia NOT NULL,
    ref_id         text           NOT NULL,
    snippet        text           NOT NULL DEFAULT '',
    fact_id        text,

    CONSTRAINT ck_evidencia_orden CHECK (orden >= 0),
    CONSTRAINT uq_evidencia_item  UNIQUE (explicacion_id, tipo, ref_id)
);

CREATE INDEX IF NOT EXISTS ix_evidencia_explicacion ON evidencia (explicacion_id, orden);
CREATE INDEX IF NOT EXISTS ix_evidencia_ref         ON evidencia (tipo, ref_id);

COMMENT ON TABLE  evidencia         IS 'Respaldo citable de cada explicación: GET /v1/evidencia/{explicacion_id}.';
COMMENT ON COLUMN evidencia.ref_id  IS 'Referencia dentro de su tipo: "441" para una línea, "PRORRATEO_PLAN" para el catálogo.';
COMMENT ON COLUMN evidencia.fact_id IS 'Campo del FactSet que ancla la cifra, p. ej. "linea:RENTA_PLAN_MOVIL.delta_cent".';


-- -----------------------------------------------------------------------------
-- gt_causa_delta (ground truth del generador sintético)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gt_causa_delta (
    gt_id         bigserial PRIMARY KEY,
    cuenta_id     text    NOT NULL,
    periodo       char(7) NOT NULL,
    concepto_id   text    NOT NULL,
    causa         tipo_movimiento,
    causa_oficial causa_oficial,
    delta_cent    bigint  NOT NULL,
    movimiento_id bigint,
    escenario     text,
    seed          bigint,
    creado_en     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_gt_periodo CHECK (periodo ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'),
    -- Un mismo concepto puede tener dos causas cuando se inyectan dos escenarios a la vez.
    CONSTRAINT uq_gt_fila UNIQUE (cuenta_id, periodo, concepto_id, causa)
);

CREATE INDEX IF NOT EXISTS ix_gt_cuenta_periodo ON gt_causa_delta (cuenta_id, periodo);
CREATE INDEX IF NOT EXISTS ix_gt_escenario      ON gt_causa_delta (escenario) WHERE escenario IS NOT NULL;

COMMENT ON TABLE  gt_causa_delta            IS 'Verdad de referencia escrita EN EL MISMO ACTO de generar el escenario, jamás deducida después.';
COMMENT ON COLUMN gt_causa_delta.delta_cent IS 'Variación atribuida a esta causa, en CÉNTIMOS. Σ por (cuenta, periodo) debe igualar total_actual - total_previo.';
COMMENT ON COLUMN gt_causa_delta.seed       IS 'Semilla del cliente: seed_cliente = sha256("seed|cuenta_id")[:8]. Hace cada caso reproducible por separado.';


-- -----------------------------------------------------------------------------
-- Vistas de conciliación — usadas por eval/ y por `make audit`
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_recibo_conciliacion AS
SELECT r.recibo_id,
       r.cuenta_id,
       r.periodo,
       r.total_cent,
       COALESCE(sum(l.monto_cent), 0)                  AS suma_lineas_cent,
       r.total_cent - COALESCE(sum(l.monto_cent), 0)   AS descuadre_cent,
       count(l.linea_id)                               AS lineas
  FROM recibo r
  LEFT JOIN recibo_linea l ON l.recibo_id = r.recibo_id
 GROUP BY r.recibo_id, r.cuenta_id, r.periodo, r.total_cent;

COMMENT ON VIEW v_recibo_conciliacion IS
    'Descuadre por recibo. Debe estar siempre a 0: el trigger diferido lo garantiza.';

CREATE OR REPLACE VIEW v_gt_conciliacion AS
SELECT g.cuenta_id,
       g.periodo,
       sum(g.delta_cent)                                       AS suma_gt_cent,
       act.total_cent - prev.total_cent                        AS delta_real_cent,
       sum(g.delta_cent) - (act.total_cent - prev.total_cent)  AS descuadre_cent
  FROM gt_causa_delta g
  JOIN recibo act  ON act.cuenta_id = g.cuenta_id AND act.periodo = g.periodo
  LEFT JOIN LATERAL (
        SELECT r.total_cent
          FROM recibo r
         WHERE r.cuenta_id = g.cuenta_id AND r.periodo < g.periodo
         ORDER BY r.periodo DESC
         LIMIT 1
  ) prev ON true
 GROUP BY g.cuenta_id, g.periodo, act.total_cent, prev.total_cent;

COMMENT ON VIEW v_gt_conciliacion IS
    'Comprueba Σ gt.delta_cent = total_actual - total_previo. El generador ABORTA si no cuadra; esta vista lo verifica ya cargado.';
