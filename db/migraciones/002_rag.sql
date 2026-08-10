-- =============================================================================
-- 002_rag.sql — corpus recuperables (catálogo, FAQs, casuísticas)
-- -----------------------------------------------------------------------------
-- REGLA INNEGOCIABLE Nº 3: **el recibo NO se vectoriza.** Aquí no hay ni una fila
-- con datos de facturación de un cliente. El recibo es consulta estructurada sobre
-- las tablas de 001_core.sql; a índice vectorial solo van tres corpus de
-- conocimiento genérico:
--
--   concepto_catalogo  lookup por clave (concepto_id viene del FactSet);
--                      el vector es secundario, solo para preguntas sueltas.
--   faq                híbrido BM25 + vectorial con fusión RRF, filtrado por los
--                      concepto_id del FactSet.
--   casuistica         vectorial por FIRMA CAUSAL: guía la estructura narrativa,
--                      no aporta cifras.
--
-- REGLA INNEGOCIABLE Nº 4: ninguna cifra recuperada de estas tablas puede llegar al
-- texto final. `packages/retriever/saneador.py` sustituye montos, porcentajes y
-- fechas concretos por marcadores («un monto», «una fecha») ANTES del prompt, y el
-- ALLOWED del verificador se construye SOLO desde el FactSet. La columna
-- `texto_saneado` guarda esa versión ya limpia para no depender de que alguien
-- recuerde sanear en tiempo de consulta.
--
-- DIMENSIÓN DEL EMBEDDING: vector(768), que es el valor por defecto de EMBED_DIM.
-- ⚠️ Cambiar de modelo de embeddings CAMBIA la dimensión y OBLIGA A REINDEXAR: hay
-- que emitir una migración nueva con ALTER TABLE ... ALTER COLUMN embedding TYPE
-- vector(N), recrear los índices HNSW y recalcular todos los vectores. Las columnas
-- `modelo_embedding` y `dim_embedding` existen para detectar vectores obsoletos, y
-- `db/migrar.py --verificar-dim` compara EMBED_DIM con el typmod real.
-- =============================================================================

-- pgvector: índice vectorial. Si no estuviera disponible, el retriever degrada a
-- BM25 puro (packages/retriever/hibrido.py) y solo pierde el canal semántico.
CREATE EXTENSION IF NOT EXISTS vector;

-- pg_trgm: búsqueda tolerante a erratas sobre firmas causales y sinónimos.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

DO $$
BEGIN
    CREATE TYPE corpus_rag AS ENUM ('concepto_catalogo', 'faq', 'casuistica');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- -----------------------------------------------------------------------------
-- Aplanado de arrays para el tsvector
-- -----------------------------------------------------------------------------
-- `array_to_string(anyarray, text)` es STABLE, no IMMUTABLE, porque la función de
-- salida de un tipo cualquiera puede depender de parámetros de sesión (una fecha
-- depende de TimeZone). Una columna GENERATED exige IMMUTABLE, así que se envuelve
-- restringida a `text[]`: para elementos de texto y un separador constante el
-- resultado es completamente determinista, y por eso la marca es correcta aquí.
CREATE OR REPLACE FUNCTION texto_de_array(p_valores text[])
RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT coalesce(array_to_string(p_valores, ' '), '');
$$;

COMMENT ON FUNCTION texto_de_array(text[]) IS
    'Aplana un text[] a texto separado por espacios. IMMUTABLE por estar restringida a text[]: se usa en columnas tsvector GENERATED.';


-- -----------------------------------------------------------------------------
-- concepto_catalogo
-- -----------------------------------------------------------------------------
-- Espejo de packages/core_domain/esquemas/recibo.py::ConceptoCatalogo. La fuente de
-- verdad sigue siendo db/reglas/rules.yaml: esta tabla es su proyección consultable.
CREATE TABLE IF NOT EXISTS concepto_catalogo (
    concepto_id        text PRIMARY KEY,
    nombre_comercial   text             NOT NULL,
    nombre_tecnico     text             NOT NULL DEFAULT '',
    familia            familia_concepto NOT NULL,
    definicion_cliente text             NOT NULL,
    definicion_tecnica text             NOT NULL DEFAULT '',
    prorrateable       boolean          NOT NULL DEFAULT false,
    afecto_igv         boolean          NOT NULL DEFAULT true,
    causas_permitidas  tipo_movimiento[] NOT NULL DEFAULT '{}',
    causa_oficial      causa_oficial,
    sinonimos          text[]           NOT NULL DEFAULT '{}',
    ejemplo_variacion  text,
    visible_cliente    boolean          NOT NULL DEFAULT true,
    rules_version      text             NOT NULL DEFAULT '1.0.0',
    modelo_embedding   text,
    dim_embedding      integer,
    embedding          vector(768),
    actualizado_en     timestamptz      NOT NULL DEFAULT now(),

    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('spanish', coalesce(nombre_comercial, '')), 'A') ||
        setweight(to_tsvector('spanish', texto_de_array(sinonimos)), 'A') ||
        setweight(to_tsvector('spanish', coalesce(nombre_tecnico, '')), 'B') ||
        setweight(to_tsvector('spanish', coalesce(definicion_cliente, '')), 'C') ||
        setweight(to_tsvector('spanish', coalesce(ejemplo_variacion, '')), 'D')
    ) STORED,

    CONSTRAINT ck_catalogo_id  CHECK (concepto_id ~ '^[A-Z0-9_]+$'),
    CONSTRAINT ck_catalogo_dim CHECK (dim_embedding IS NULL OR dim_embedding = 768)
);

CREATE INDEX IF NOT EXISTS ix_catalogo_fts     ON concepto_catalogo USING gin (fts);
CREATE INDEX IF NOT EXISTS ix_catalogo_familia ON concepto_catalogo (familia);
CREATE INDEX IF NOT EXISTS ix_catalogo_sinon   ON concepto_catalogo USING gin (sinonimos);
CREATE INDEX IF NOT EXISTS ix_catalogo_causas  ON concepto_catalogo USING gin (causas_permitidas);
CREATE INDEX IF NOT EXISTS ix_catalogo_emb_hnsw
    ON concepto_catalogo USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMENT ON TABLE  concepto_catalogo                    IS 'Definición de cada concepto facturable en lenguaje de cliente. Se accede por CLAVE; el vector es secundario.';
COMMENT ON COLUMN concepto_catalogo.definicion_cliente IS 'Explicación simple, de usted, sin tecnicismos. Es lo que se le muestra al cliente en GET /v1/catalogo/{concepto_id}.';
COMMENT ON COLUMN concepto_catalogo.causas_permitidas  IS 'Movimientos que pueden explicar este concepto (tabla regla_concepto_causa de rules.yaml). Acota la atribución.';
COMMENT ON COLUMN concepto_catalogo.prorrateable       IS 'Si admite cálculo por días. El financiamiento NUNCA se prorratea.';
COMMENT ON COLUMN concepto_catalogo.fts                IS 'tsvector en español con pesos: nombre y sinónimos A, técnico B, definición C, ejemplo D.';
COMMENT ON COLUMN concepto_catalogo.embedding          IS 'Vector de 768 dimensiones (EMBED_DIM). Cambiar de modelo obliga a reindexar TODO el corpus.';
COMMENT ON COLUMN concepto_catalogo.modelo_embedding   IS 'Modelo que generó el vector; detecta vectores obsoletos tras un cambio de proveedor.';


-- -----------------------------------------------------------------------------
-- faq
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS faq (
    faq_id           text PRIMARY KEY,
    pregunta         text        NOT NULL,
    respuesta        text        NOT NULL,
    texto_saneado    text        NOT NULL DEFAULT '',
    conceptos        text[]      NOT NULL DEFAULT '{}',
    causas           tipo_movimiento[] NOT NULL DEFAULT '{}',
    etiquetas        text[]      NOT NULL DEFAULT '{}',
    canal_sugerido   canal,
    origen           text        NOT NULL DEFAULT 'seed',
    activo           boolean     NOT NULL DEFAULT true,
    modelo_embedding text,
    dim_embedding    integer,
    embedding        vector(768),
    actualizado_en   timestamptz NOT NULL DEFAULT now(),

    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('spanish', coalesce(pregunta, '')), 'A') ||
        setweight(to_tsvector('spanish', texto_de_array(etiquetas)), 'B') ||
        setweight(to_tsvector('spanish', coalesce(respuesta, '')), 'C')
    ) STORED,

    CONSTRAINT ck_faq_dim CHECK (dim_embedding IS NULL OR dim_embedding = 768)
);

CREATE INDEX IF NOT EXISTS ix_faq_fts       ON faq USING gin (fts);
-- El retriever filtra las FAQs por los concepto_id del FactSet antes de fusionar:
-- este índice GIN sobre el array es el que hace barato ese filtro.
CREATE INDEX IF NOT EXISTS ix_faq_conceptos ON faq USING gin (conceptos);
CREATE INDEX IF NOT EXISTS ix_faq_causas    ON faq USING gin (causas);
CREATE INDEX IF NOT EXISTS ix_faq_activo    ON faq (activo) WHERE activo;
CREATE INDEX IF NOT EXISTS ix_faq_emb_hnsw
    ON faq USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMENT ON TABLE  faq               IS 'Preguntas frecuentes anonimizadas. Recuperación híbrida BM25 + vectorial con fusión RRF (k=60).';
COMMENT ON COLUMN faq.texto_saneado IS 'Respuesta con las cifras ya sustituidas por marcadores («un monto», «una fecha»). ES LA ÚNICA VERSIÓN QUE PUEDE ENTRAR AL PROMPT.';
COMMENT ON COLUMN faq.conceptos     IS 'concepto_id a los que aplica. El retriever filtra por los del FactSet: una FAQ de roaming no explica un cambio de plan.';
COMMENT ON COLUMN faq.fts           IS 'tsvector en español: pregunta A, etiquetas B, respuesta C.';


-- -----------------------------------------------------------------------------
-- casuistica
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS casuistica (
    casuistica_id    text PRIMARY KEY,
    titulo           text        NOT NULL,
    firma_causal     text        NOT NULL,
    modalidad_renta  modalidad_renta,
    signo_delta      smallint,
    narrativa        text        NOT NULL,
    texto_saneado    text        NOT NULL DEFAULT '',
    estructura       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    conceptos        text[]      NOT NULL DEFAULT '{}',
    causas           tipo_movimiento[] NOT NULL DEFAULT '{}',
    origen           text        NOT NULL DEFAULT 'seed',
    activo           boolean     NOT NULL DEFAULT true,
    modelo_embedding text,
    dim_embedding    integer,
    embedding        vector(768),
    actualizado_en   timestamptz NOT NULL DEFAULT now(),

    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('spanish', coalesce(titulo, '')), 'A') ||
        setweight(to_tsvector('spanish', coalesce(firma_causal, '')), 'B') ||
        setweight(to_tsvector('spanish', coalesce(narrativa, '')), 'C')
    ) STORED,

    CONSTRAINT ck_casuistica_signo CHECK (signo_delta IS NULL OR signo_delta IN (-1, 0, 1)),
    CONSTRAINT ck_casuistica_estructura CHECK (jsonb_typeof(estructura) = 'array'),
    CONSTRAINT ck_casuistica_dim   CHECK (dim_embedding IS NULL OR dim_embedding = 768)
);

-- La firma causal es la clave de acceso principal: FactSet.firma_causal() devuelve
-- "CAMBIO_PLAN#ADELANTADA#+" y se busca por igualdad; el índice trigram cubre la
-- coincidencia parcial cuando la combinación exacta no existe en el corpus.
CREATE INDEX IF NOT EXISTS ix_casuistica_firma      ON casuistica (firma_causal);
CREATE INDEX IF NOT EXISTS ix_casuistica_firma_trgm ON casuistica USING gin (firma_causal gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_casuistica_fts        ON casuistica USING gin (fts);
CREATE INDEX IF NOT EXISTS ix_casuistica_conceptos  ON casuistica USING gin (conceptos);
CREATE INDEX IF NOT EXISTS ix_casuistica_modalidad  ON casuistica (modalidad_renta, signo_delta);
CREATE INDEX IF NOT EXISTS ix_casuistica_emb_hnsw
    ON casuistica USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMENT ON TABLE  casuistica                 IS 'Patrones narrativos por combinación de causas. Guían CÓMO se cuenta, nunca CUÁNTO: no aportan ni una cifra.';
COMMENT ON COLUMN casuistica.firma_causal    IS 'causas ordenadas + modalidad + signo(Δ), p. ej. "CAMBIO_PLAN#ADELANTADA#+". Lo produce FactSet.firma_causal().';
COMMENT ON COLUMN casuistica.signo_delta     IS '+1 el recibo subió, -1 bajó, 0 no varió (escenario ESTABLE).';
COMMENT ON COLUMN casuistica.estructura      IS 'Orden de bloques sugerido para la respuesta, p. ej. ["puente", "tabla_tramos", "aviso"].';
COMMENT ON COLUMN casuistica.texto_saneado   IS 'Narrativa sin cifras concretas. Es la única versión que puede entrar al prompt.';


-- -----------------------------------------------------------------------------
-- Salud del índice vectorial — la consulta la usa `make audit` y el arranque de la API
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_rag_salud AS
SELECT 'concepto_catalogo'::corpus_rag AS corpus,
       count(*)                        AS documentos,
       count(embedding)                AS vectorizados,
       count(*) FILTER (WHERE embedding IS NULL) AS sin_vector,
       count(DISTINCT modelo_embedding)          AS modelos_distintos
  FROM concepto_catalogo
UNION ALL
SELECT 'faq', count(*), count(embedding),
       count(*) FILTER (WHERE embedding IS NULL), count(DISTINCT modelo_embedding)
  FROM faq WHERE activo
UNION ALL
SELECT 'casuistica', count(*), count(embedding),
       count(*) FILTER (WHERE embedding IS NULL), count(DISTINCT modelo_embedding)
  FROM casuistica WHERE activo;

COMMENT ON VIEW v_rag_salud IS
    'Cobertura del índice vectorial por corpus. modelos_distintos > 1 significa vectores mezclados de dos modelos: hay que reindexar.';
