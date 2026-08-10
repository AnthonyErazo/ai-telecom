-- =============================================================================
-- 003_auditoria.sql — bitácora append-only con cadena de hashes
-- -----------------------------------------------------------------------------
-- Espejo persistente de packages/governance/auditoria.py. La ficha del Desafío 1
-- exige *"cero invenciones financieras COMPROBABLES MEDIANTE LOGS DE LA TERMINAL"*:
-- el JSONL es la evidencia que se proyecta en la demo y esta tabla es la misma
-- evidencia consultable.
--
--     hash_n = SHA256(hash_{n-1} || json_canonico(evento_n))
--
-- Tres capas de protección, en orden de fuerza creciente:
--
--   1. REVOKE UPDATE, DELETE, TRUNCATE — bloquea al rol de aplicación. El PROPIETARIO
--      de la tabla conserva sus privilegios implícitos, así que por sí solo no basta.
--   2. Triggers que abortan cualquier UPDATE, DELETE o TRUNCATE — alcanzan también al
--      propietario. Solo un superusuario podría deshabilitarlos, y eso deja rastro.
--   3. Un CHECK que recalcula el hash con `sha256()` del núcleo de PostgreSQL: aunque
--      alguien lograse insertar una fila inventada, la base la rechaza salvo que el
--      hash cuadre con el eslabón anterior. Falsificar un evento obliga a reescribir
--      TODOS los posteriores, y `auditoria_verificar_cadena()` detecta el corte.
--
-- La columna `canonico` guarda el JSON canónico exacto que produjo Python
-- (`EventoAuditoria.json_canonico()`: claves ordenadas, sin espacios, sin el campo
-- `hash`). No se recalcula desde `payload` porque la normalización de jsonb reordena
-- claves y números: el hash se comprueba sobre el texto original, byte a byte.
-- =============================================================================

DO $$
BEGIN
    CREATE TYPE etapa_auditoria AS ENUM (
        'REQUEST', 'FACTS_BUILT', 'INVARIANTE', 'RETRIEVE', 'ROUTE',
        'LLM_CALL', 'VERIFY', 'CITATIONS', 'RESPONSE', 'CHAIN');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- -----------------------------------------------------------------------------
-- Recálculo del eslabón: sha256() es núcleo de PostgreSQL (>= 11), sin extensiones
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION auditoria_hash_esperado(p_hash_prev text, p_canonico text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    SELECT encode(sha256(convert_to(p_hash_prev || p_canonico, 'UTF8')), 'hex');
$$;

COMMENT ON FUNCTION auditoria_hash_esperado(text, text) IS
    'SHA256(hash_previo || json_canonico) en hexadecimal. Réplica exacta de EventoAuditoria.calcular_hash().';

CREATE OR REPLACE FUNCTION auditoria_hash_genesis()
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT repeat('0', 64);
$$;

COMMENT ON FUNCTION auditoria_hash_genesis() IS
    'Hash convencional anterior al primer evento (HASH_GENESIS).';


-- -----------------------------------------------------------------------------
-- auditoria_evento
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auditoria_evento (
    cadena              text            NOT NULL DEFAULT 'principal',
    indice              bigint          NOT NULL,
    trace_id            text            NOT NULL,
    etapa               etapa_auditoria NOT NULL,
    ts                  timestamptz     NOT NULL DEFAULT now(),
    actor               text,
    cuenta_ref          text,
    acting_on_behalf_of text,
    nivel               nivel_aseguramiento,
    payload             jsonb           NOT NULL DEFAULT '{}'::jsonb,
    canonico            text            NOT NULL,
    hash_prev           char(64)        NOT NULL,
    hash                char(64)        NOT NULL,
    registrado_en       timestamptz     NOT NULL DEFAULT now(),

    CONSTRAINT pk_auditoria_evento PRIMARY KEY (cadena, indice),
    CONSTRAINT uq_auditoria_hash   UNIQUE (hash),
    CONSTRAINT ck_auditoria_indice CHECK (indice >= 0),
    CONSTRAINT ck_auditoria_hex    CHECK (hash ~ '^[0-9a-f]{64}$' AND hash_prev ~ '^[0-9a-f]{64}$'),
    -- El primer eslabón, y solo él, cuelga del hash génesis.
    CONSTRAINT ck_auditoria_genesis CHECK (indice > 0 OR hash_prev = auditoria_hash_genesis()),
    CONSTRAINT ck_auditoria_payload CHECK (jsonb_typeof(payload) = 'object'),
    -- Un evento cuyo hash no cuadra con su contenido no entra. Esto es lo que hace
    -- que la bitácora sea una prueba y no un registro de depuración.
    CONSTRAINT ck_auditoria_hash_valido CHECK (hash = auditoria_hash_esperado(hash_prev, canonico)),
    -- LOA_ASESOR obliga a declarar en nombre de quién se actúa (sección 9 de la spec).
    CONSTRAINT ck_auditoria_asesor CHECK (
        nivel IS DISTINCT FROM 'LOA_ASESOR' OR acting_on_behalf_of IS NOT NULL)
);

-- Índice por turno: es la consulta de GET /v1/auditoria?trace_id.
CREATE INDEX IF NOT EXISTS ix_auditoria_trace  ON auditoria_evento (trace_id, indice);
-- Índice por entidad: "todo lo que se hizo sobre esta cuenta", para atención y ARCO.
CREATE INDEX IF NOT EXISTS ix_auditoria_cuenta ON auditoria_evento (cuenta_ref, ts DESC)
    WHERE cuenta_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_auditoria_etapa  ON auditoria_evento (etapa, ts DESC);
CREATE INDEX IF NOT EXISTS ix_auditoria_ts     ON auditoria_evento (ts DESC);
-- Consultas sobre el detalle de aserciones del evento VERIFY.
CREATE INDEX IF NOT EXISTS ix_auditoria_payload ON auditoria_evento USING gin (payload jsonb_path_ops);
-- Actuaciones de asesores: se auditan aparte por exigencia del nivel LOA_ASESOR.
CREATE INDEX IF NOT EXISTS ix_auditoria_asesor ON auditoria_evento (acting_on_behalf_of, ts DESC)
    WHERE acting_on_behalf_of IS NOT NULL;

COMMENT ON TABLE  auditoria_evento                     IS 'Bitácora APPEND-ONLY encadenada por hash. Una fila por etapa del pipeline. No se actualiza ni se borra jamás.';
COMMENT ON COLUMN auditoria_evento.cadena              IS 'Nombre del flujo encadenado; permite una bitácora por entorno sin mezclar índices.';
COMMENT ON COLUMN auditoria_evento.indice              IS 'Posición en la cadena, consecutiva desde 0. Un hueco es una manipulación.';
COMMENT ON COLUMN auditoria_evento.trace_id            IS 'Turno al que pertenece el evento; agrupa las diez etapas de una explicación.';
COMMENT ON COLUMN auditoria_evento.cuenta_ref          IS 'Referencia TOKENIZADA de la cuenta. Se deriva del token de sesión, nunca del texto del usuario.';
COMMENT ON COLUMN auditoria_evento.acting_on_behalf_of IS 'Cliente en cuyo nombre actúa un asesor. Obligatorio en LOA_ASESOR, y el CHECK lo impone.';
COMMENT ON COLUMN auditoria_evento.payload             IS 'Contenido de la etapa. FACTS_BUILT incluye residual_cent; VERIFY, la lista completa de aserciones con estado y fuente.';
COMMENT ON COLUMN auditoria_evento.canonico            IS 'JSON canónico EXACTO sobre el que se calculó el hash (sin el campo hash). No derivarlo de payload: jsonb reordena.';
COMMENT ON COLUMN auditoria_evento.hash_prev           IS 'Hash del evento anterior de la cadena; en el primero, 64 ceros.';
COMMENT ON COLUMN auditoria_evento.hash                IS 'SHA256(hash_prev || canonico). Verificado por CHECK en cada INSERT.';


-- -----------------------------------------------------------------------------
-- Append-only: triggers que alcanzan también al propietario de la tabla
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_auditoria_append_only()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'auditoria_evento es append-only: la operación % está prohibida', TG_OP
        USING ERRCODE = 'insufficient_privilege',
              HINT = 'Para corregir un evento, emita uno nuevo. La cadena de hashes es la prueba: no se reescribe.';
END $$;

COMMENT ON FUNCTION fn_auditoria_append_only() IS
    'Aborta UPDATE, DELETE y TRUNCATE sobre la bitácora. Complementa al REVOKE, que no alcanza al propietario.';

DROP TRIGGER IF EXISTS tg_auditoria_no_modificar ON auditoria_evento;
CREATE TRIGGER tg_auditoria_no_modificar
    BEFORE UPDATE OR DELETE ON auditoria_evento
    FOR EACH ROW EXECUTE FUNCTION fn_auditoria_append_only();

DROP TRIGGER IF EXISTS tg_auditoria_no_truncar ON auditoria_evento;
CREATE TRIGGER tg_auditoria_no_truncar
    BEFORE TRUNCATE ON auditoria_evento
    FOR EACH STATEMENT EXECUTE FUNCTION fn_auditoria_append_only();


-- -----------------------------------------------------------------------------
-- Privilegios: la bitácora solo admite INSERT y SELECT
-- -----------------------------------------------------------------------------
REVOKE UPDATE, DELETE, TRUNCATE ON auditoria_evento FROM PUBLIC;
GRANT SELECT, INSERT ON auditoria_evento TO PUBLIC;

-- También al propietario: sus privilegios son implícitos pero revocables. Puede
-- volver a concedérselos, y ese GRANT queda registrado en los logs del servidor.
DO $$
BEGIN
    EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON auditoria_evento FROM %I', current_user);
EXCEPTION WHEN insufficient_privilege OR undefined_object THEN
    RAISE NOTICE 'no se pudo revocar al usuario actual (%); quedan los triggers append-only', current_user;
END $$;


-- -----------------------------------------------------------------------------
-- Verificación de la cadena en SQL
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION auditoria_verificar_cadena(p_cadena text DEFAULT 'principal')
RETURNS TABLE (valida boolean, indice_roto bigint)
LANGUAGE sql
STABLE
AS $$
    WITH ordenado AS (
        SELECT e.indice,
               e.hash,
               e.hash_prev,
               e.canonico,
               lag(e.hash)   OVER (ORDER BY e.indice) AS hash_anterior,
               lag(e.indice) OVER (ORDER BY e.indice) AS indice_anterior
          FROM auditoria_evento e
         WHERE e.cadena = p_cadena
    ),
    roto AS (
        SELECT min(o.indice) AS indice
          FROM ordenado o
         WHERE o.hash <> auditoria_hash_esperado(o.hash_prev, o.canonico)
            OR o.hash_prev IS DISTINCT FROM COALESCE(o.hash_anterior, auditoria_hash_genesis())
            OR o.indice   IS DISTINCT FROM COALESCE(o.indice_anterior + 1, 0)
    )
    SELECT (SELECT r.indice FROM roto r) IS NULL,
           (SELECT r.indice FROM roto r);
$$;

COMMENT ON FUNCTION auditoria_verificar_cadena(text) IS
    'Recalcula la cadena y devuelve (valida, indice_roto). Equivale a packages.governance.auditoria.verificar_cadena().';


-- -----------------------------------------------------------------------------
-- Vista por turno — alimenta GET /v1/auditoria y las métricas de eval/
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_auditoria_turno AS
SELECT e.trace_id,
       min(e.ts)                                   AS inicio,
       max(e.ts)                                   AS fin,
       count(*)                                    AS eventos,
       min(e.indice)                               AS indice_inicial,
       max(e.indice)                               AS indice_final,
       max(e.cuenta_ref)                           AS cuenta_ref,
       max(e.acting_on_behalf_of)                  AS acting_on_behalf_of,
       bool_or(e.etapa = 'VERIFY')                 AS verificado,
       max(e.payload ->> 'veredicto')
           FILTER (WHERE e.etapa = 'VERIFY')       AS veredicto,
       max(CASE WHEN jsonb_typeof(e.payload -> 'aserciones_totales') = 'number'
                THEN (e.payload ->> 'aserciones_totales')::bigint END)
           FILTER (WHERE e.etapa = 'VERIFY')       AS aserciones_totales,
       max(CASE WHEN jsonb_typeof(e.payload -> 'aserciones_no_ancladas') = 'number'
                THEN (e.payload ->> 'aserciones_no_ancladas')::bigint END)
           FILTER (WHERE e.etapa = 'VERIFY')       AS aserciones_no_ancladas,
       max(CASE WHEN jsonb_typeof(e.payload -> 'residual_cent') = 'number'
                THEN (e.payload ->> 'residual_cent')::bigint END)
           FILTER (WHERE e.etapa = 'FACTS_BUILT')  AS residual_cent,
       bool_or(e.etapa = 'ROUTE' AND e.payload -> 'derivar' = 'true'::jsonb) AS derivada
  FROM auditoria_evento e
 GROUP BY e.trace_id;

COMMENT ON VIEW v_auditoria_turno IS
    'Un renglón por turno con su veredicto de verificación, su residual y si terminó en derivación.';
