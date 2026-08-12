-- =============================================================================
-- recibo-claro · ESQUEMA COMPLETO
-- -----------------------------------------------------------------------------
-- UN SOLO FICHERO, idempotente. Pegar en Supabase -> SQL Editor -> Run, o aplicar con
-- `python -m db.migrar`. Los datos NO están aquí: se cargan con
-- `scripts/cargar_supabase.py`.
--
-- Solo hay tablas que algo escribe y algo lee. Se quitaron trece que espejaban en
-- PostgreSQL el modelo de dominio —`recibo`, `recibo_linea`, `movimiento`, `pago`,
-- `cuenta`, `cliente`, `factset`, `explicacion`...— de cuando el diseño pretendía
-- persistir la facturación entera. El motor lee `cargo_facturado`, que ES el dataset del
-- desafío, y construye el modelo canónico en memoria; aquellas tablas nunca se
-- escribieron ni se consultaron.
-- =============================================================================

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
-- COMPROBADO contra la API real y contra la base real: `gemini-embedding-001` devuelve
-- vectores de 768 componentes porque `EMBED_DIM` no está en `.env` y el embedder pide
-- `output_dimensionality=768`; el typmod de las tres columnas `embedding` es 768. NO
-- hay desajuste de dimensión y no hay que tocar nada aquí. Se deja escrito porque la
-- dimensión fue la primera sospechosa del «RAG vacío» y no era: la causa era que nadie
-- escribía en estas columnas. `gemini-embedding-001` es un modelo MRL (nativo 3072) y
-- el propio API trunca a la dimensión pedida; `normalizar_vector()` renormaliza después,
-- que es exactamente lo que Google recomienda al truncar y lo que permite tratar el
-- coseno como producto escalar.
-- ⚠️ Cambiar de modelo de embeddings PUEDE cambiar la dimensión y OBLIGA A REINDEXAR.
-- Si la dimensión cambia hay que emitir una migración nueva con ALTER TABLE ... ALTER
-- COLUMN embedding TYPE vector(N) y recrear los índices HNSW. Si NO cambia, el DDL se
-- queda igual pero HAY QUE RECALCULAR TODOS LOS VECTORES IGUAL, y esta es la trampa: dos
-- modelos de la misma dimensión producen vectores que caben en la misma columna y no
-- son comparables entre sí. La base los acepta sin rechistar y el error no se manifiesta
-- como un fallo, sino como resultados peores que nadie sabe explicar.
--
-- MODELO EN USO HOY (verificado contra la base el 12/08/2026): `gemini-embedding-2`,
-- firma `gemini:gemini-embedding-2:768`, en las 662 filas de las tres tablas. Se cambió
-- desde `gemini-embedding-001` porque la cuota diaria del nivel gratuito es POR MODELO
-- (1.000 textos/día) y la de `-001` se agotó a mitad de la primera indexación. La
-- dimensión sigue siendo 768, así que este DDL no se tocó.
--
-- Ese cambio dejó, durante unas horas, `casuistica` con vectores de los dos modelos a la
-- vez (`v_rag_salud.modelos_distintos = 2`). Se resolvió solo, y por diseño: el criterio
-- de «pendiente» de `scripts/vectorizar_corpus.py` no es «embedding IS NULL» sino
-- «embedding IS NULL **O** modelo_embedding distinto de la firma actual», de modo que
-- cambiar de modelo marca como pendiente lo que quedó obsoleto y la siguiente pasada lo
-- reescribe. Por eso `modelo_embedding` y `dim_embedding` no son metadatos decorativos:
-- son el mecanismo que hace detectable —y reparable— la mezcla. Las consultas de
-- similitud filtran además por `modelo_embedding = <firma>`, para que una mezcla en
-- curso devuelva menos resultados en vez de resultados incomparables.
-- `db/migrar.py --verificar-dim` compara EMBED_DIM con el typmod real.
-- =============================================================================

-- pgvector: índice vectorial. Si no estuviera disponible, el retriever degrada a
-- BM25 puro (packages/retriever/hibrido.py) y solo pierde el canal semántico.
CREATE EXTENSION IF NOT EXISTS vector;

-- pg_trgm: búsqueda tolerante a erratas sobre firmas causales y sinónimos.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

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
COMMENT ON COLUMN casuistica.firma_causal    IS 'causas ordenadas + modalidad + signo(Δ), p. ej. "CAMBIO_PLAN#ADELANTADA#+". Lo produce FactSet.firma_causal().';
COMMENT ON COLUMN casuistica.estructura      IS 'Orden de bloques sugerido para la respuesta, p. ej. ["puente", "tabla_tramos", "aviso"].';
COMMENT ON COLUMN casuistica.texto_saneado   IS 'Narrativa sin cifras concretas. Es la única versión que puede entrar al prompt.';

-- -----------------------------------------------------------------------------
-- vocabulario_peruano
-- -----------------------------------------------------------------------------
-- Esta tabla EXISTÍA en Supabase con 240 filas y NO estaba declarada en este fichero:
-- alguien la creó a mano y el esquema dejó de ser la única verdad sin que se notara.
-- Se declara aquí, tal y como está en la base, para que volver a levantar el proyecto
-- desde cero reproduzca lo que hay hoy. `CREATE TABLE IF NOT EXISTS` la deja intacta.
--
-- Qué es: la jerga con la que un cliente peruano dice las cosas. «Cancelar» en Perú es
-- PAGAR, no dar de baja; el cliente dice «recibo» y nunca «factura». Lo consume
-- `packages/facts_engine/jerga.py` para normalizar la pregunta ANTES de clasificar la
-- intención. Confundir «ya cancelé» con una baja es el error más caro del dominio.
--
-- Por qué lleva embedding si no es un corpus del recuperador: las `variantes` son las
-- formas reales en que se escribe cada término («ya cancele», «cancelé», «cancelado»).
-- La coincidencia exacta falla con la ortografía real de un chat; la similitud coseno
-- no. El vector se guarda pegado a la fila por la misma razón que en `faq`: para
-- filtrar y ordenar en la misma consulta SQL, sin un segundo almacén que sincronizar.
CREATE TABLE IF NOT EXISTS vocabulario_peruano (
    termino          text PRIMARY KEY,
    significa        text        NOT NULL DEFAULT '',
    concepto_id      text,
    procedencia      text,
    nota             text,
    variantes        text[]      NOT NULL DEFAULT '{}',
    modelo_embedding text,
    dim_embedding    integer,
    embedding        vector(768),
    actualizado_en   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_vocabulario_dim CHECK (dim_embedding IS NULL OR dim_embedding = 768)
);

-- Trigram sobre el término: el cliente escribe sin tildes y con erratas.
CREATE INDEX IF NOT EXISTS ix_vocab_trgm ON vocabulario_peruano USING gin (termino gin_trgm_ops);
-- HNSW: faltaba. `faq` y `casuistica` tenían su índice vectorial desde el principio y
-- esta tabla no, así que su búsqueda por similitud era un recorrido secuencial. Con 240
-- filas no dolía, pero el plan cambia al crecer y es justo el tipo de deuda que no
-- avisa.
CREATE INDEX IF NOT EXISTS ix_vocab_emb_hnsw
    ON vocabulario_peruano USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMENT ON TABLE  vocabulario_peruano             IS 'Jerga peruana de facturación. Normaliza la pregunta del cliente antes de clasificar la intención.';
COMMENT ON COLUMN vocabulario_peruano.variantes   IS 'Formas reales de escribir el término, con y sin tilde. Son la señal que recupera el vector.';
COMMENT ON COLUMN vocabulario_peruano.procedencia IS 'De dónde sale el término: USO_PERUANO, TRANSCRIPCION (del vídeo «Planta»)…';

-- Cobertura del índice vectorial, un corpus por fila. Se declara sobre las TRES tablas
-- que de verdad se vectorizan: `faq`, `casuistica` y `vocabulario_peruano`. El catálogo
-- de conceptos salió de aquí porque ya no es una tabla —se deriva del propio dataset en
-- `v_concepto_real`— y contarlo como corpus indexable daba una salud falsa.
--
-- `vocabulario_peruano` se añade porque, mientras no estuvo, la vista decía que el RAG
-- estaba sano sin mirar 240 de sus 662 documentos. Una métrica de salud que no cubre un
-- tercio del corpus es peor que ninguna: da confianza falsa.
CREATE OR REPLACE VIEW v_rag_salud AS
SELECT 'faq'        AS corpus,
       count(*)                                   AS documentos,
       count(embedding)                           AS vectorizados,
       count(DISTINCT modelo_embedding)           AS modelos_distintos
FROM faq
UNION ALL
SELECT 'casuistica',
       count(*), count(embedding), count(DISTINCT modelo_embedding)
FROM casuistica
UNION ALL
SELECT 'vocabulario_peruano',
       count(*), count(embedding), count(DISTINCT modelo_embedding)
FROM vocabulario_peruano;

-- Campos que el corpus de casuísticas necesita y que la primera versión colapsaba en
-- `narrativa`. Se llaman como en el fichero JSON —`situacion`, `error_frecuente`— para
-- que el lector reuse `_ALIAS_CASUISTICA` y no haya un segundo mapeo que desincronizar.
ALTER TABLE casuistica ADD COLUMN IF NOT EXISTS situacion       text;
ALTER TABLE casuistica ADD COLUMN IF NOT EXISTS error_frecuente text;
ALTER TABLE casuistica ADD COLUMN IF NOT EXISTS accion_sugerida text;
ALTER TABLE casuistica ADD COLUMN IF NOT EXISTS senales_cliente text[] NOT NULL DEFAULT '{}';
ALTER TABLE casuistica ADD COLUMN IF NOT EXISTS prioridad       smallint NOT NULL DEFAULT 100;

COMMENT ON COLUMN casuistica.situacion       IS 'Qué le pasa al cliente, en sus términos. El corpus lo mapea a `descripcion`.';
COMMENT ON COLUMN casuistica.error_frecuente IS 'El malentendido típico que hay que desactivar. El corpus lo mapea a `advertencia`.';
COMMENT ON COLUMN casuistica.accion_sugerida IS 'Qué ofrecer al cerrar, si la explicación no basta.';
COMMENT ON COLUMN casuistica.senales_cliente IS 'Frases con las que el cliente enuncia esta situación. Alimentan BM25.';

COMMENT ON VIEW v_rag_salud IS
    'Cobertura del índice vectorial por corpus. modelos_distintos > 1 significa vectores mezclados de dos modelos: hay que reindexar.';
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
