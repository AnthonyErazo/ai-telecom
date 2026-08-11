# =============================================================================
# recibo-claro — Hackathon AI Telecom 2026, Desafío 1
#
#   make dev     arranca en local SIN Docker y SIN PostgreSQL, y abre la interfaz
#   make probar  comprueba de punta a punta la API ya levantada (PASA / FALLA)
#   make demo    levanta todo con Docker y comprueba que la explicación es VERIFICADA
#   make help    lista de objetivos
#
# Reparto de responsabilidades:
#   · Los objetivos de infraestructura (up, down, migrate, seed, indexar,
#     logs, smoke) corren en Docker: no exigen nada instalado en el equipo
#     salvo Docker.
#   · Los de desarrollo (dev, probar, test, lint, fmt, eval) corren con el
#     intérprete local, porque la imagen de runtime no lleva pytest ni ruff a
#     propósito. Prepare el entorno con `make instalar`.
#
# Portabilidad: el entorno de trabajo es Windows con Git Bash. Todo lo que sea
# dependiente de plataforma (abrir el navegador, rutas, esperas) vive en
# `scripts/dev.py` y `scripts/probar_e2e.py`, que usan solo la biblioteca
# estándar de Python y corren igual en Windows, macOS y Linux. Las recetas de
# este fichero se limitan a `$(PY) scripts/…`.
# =============================================================================

COMPOSE   ?= docker compose
PY        ?= python

# Servicio sobre el que actuan `shell` y `smoke`.
SERVICIO  ?= api

# Servicios cuyos logs sigue `make logs`. Vacio = todos.
#   make logs SERVICIOS=api
SERVICIOS ?=

# Parámetros de la demo. Coinciden con .env.example y con el dataset publicado.
SEED      ?= 20260804
CLIENTES  ?= 300
PERIODO   ?= 2026-07
CUENTA    ?= C-DEMO-01
API       ?= http://127.0.0.1:8000
LINEAS    ?= 120

# Dónde escucha `make dev`.  make dev PUERTO=8010
HOST      ?= 127.0.0.1
PUERTO    ?= 8000

# Argumentos extra para test / eval / indexar / probar:
#   make eval ARGS="--markdown"      make probar ARGS="--json"
ARGS      ?=

# Argumentos extra para `make dev`:  make dev DEV_ARGS="--sin-navegador"
DEV_ARGS  ?=

# Cómo se invoca la prueba de humo. Por defecto, dentro del contenedor de la
# API (no exige curl, jq ni Python en el equipo). Para una API local:
#   make smoke SMOKE="$(PY) docker/smoke.py"
SMOKE     ?= $(COMPOSE) exec -T $(SERVICIO) python /app/docker/smoke.py
SMOKE_API ?= http://127.0.0.1:8000

.DEFAULT_GOAL := help
.PHONY: help instalar dev probar build up down migrate seed indexar demo smoke eval golden audit \
        test lint fmt logs ps shell limpiar limpiar-datos

# --------------------------------------------------------------------------- #
# Ayuda
# --------------------------------------------------------------------------- #
help:  ## Muestra esta ayuda
	@echo "recibo-claro — objetivos disponibles"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Sin Docker:         make dev   (y en otra terminal: make probar)"
	@echo "Arranque completo:  make demo"
	@echo ""
	@echo "Sin 'make' instalado (Windows no lo trae) los dos primeros son:"
	@echo "  python scripts/dev.py        y        python scripts/probar_e2e.py"

instalar:  ## Instala el proyecto y las dependencias de desarrollo en el equipo
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev]"

# --------------------------------------------------------------------------- #
# Desarrollo local: sin Docker, sin PostgreSQL, sin red
# --------------------------------------------------------------------------- #
dev:  ## Arranca en local: dataset si falta + uvicorn con recarga + abre /ui
	$(PY) scripts/dev.py --host $(HOST) --puerto $(PUERTO) \
	  --seed $(SEED) --clientes $(CLIENTES) --periodo $(PERIODO) $(DEV_ARGS)

probar:  ## Comprueba de punta a punta la API levantada e imprime PASA/FALLA por paso
	$(PY) scripts/probar_e2e.py --api $(API) --cuenta $(CUENTA) --periodo $(PERIODO) $(ARGS)

# --------------------------------------------------------------------------- #
# Infraestructura
# --------------------------------------------------------------------------- #
build:  ## Construye la imagen de la API y de los mocks
	$(COMPOSE) build

up:  ## Levanta db + api + los dos mocks y espera a que estén sanos
	$(COMPOSE) up -d --build --wait
	@echo ""
	@echo "  API      $(API)/docs"
	@echo "  BrainyBill  http://127.0.0.1:8801/salud"
	@echo "  Amdocs      http://127.0.0.1:8802/salud"

down:  ## Detiene los servicios (conserva la base de datos)
	$(COMPOSE) down

ps:  ## Estado de los contenedores
	$(COMPOSE) ps

logs:  ## Sigue los logs (make logs SERVICIOS=api para uno solo)
	$(COMPOSE) logs -f --tail=$(LINEAS) $(SERVICIOS)

shell:  ## Abre una shell dentro del contenedor de la API
	$(COMPOSE) exec $(SERVICIO) /bin/bash

# --------------------------------------------------------------------------- #
# Datos y esquema
# --------------------------------------------------------------------------- #
migrate:  ## Aplica las migraciones SQL (idempotente)
	$(COMPOSE) up -d --wait db
	$(COMPOSE) run --rm --no-deps $(SERVICIO) python -m db.migrar

seed:  ## Genera el dataset sintético determinístico en data/sintetico
	$(COMPOSE) run --rm --no-deps $(SERVICIO) \
	  python -m packages.datagen.generar \
	    --seed $(SEED) --clientes $(CLIENTES) \
	    --periodo-actual $(PERIODO) --salida data/sintetico

indexar:  ## Indexa catálogo, FAQs y casuísticas en pgvector (idempotente)
	$(COMPOSE) up -d --wait db
	$(COMPOSE) run --rm --no-deps $(SERVICIO) \
	  python -m packages.retriever.indexar --verbose $(ARGS)

# --------------------------------------------------------------------------- #
# Demo
# --------------------------------------------------------------------------- #
demo:  ## Todo en un comando: build → migrate → seed → indexar → up → smoke
	@echo "==> 1/5 construyendo la imagen"
	$(MAKE) build
	@echo "==> 2/5 aplicando migraciones"
	$(MAKE) migrate
	@echo "==> 3/5 generando el dataset sintético (semilla $(SEED))"
	$(MAKE) seed
	@echo "==> 4/5 indexando el corpus RAG"
	$(MAKE) indexar
	@echo "==> 5/5 levantando la API y los mocks"
	$(MAKE) up
	@echo ""
	$(MAKE) smoke

# El orden importa: la API cachea el corpus RAG al arrancar, así que se siembra
# e indexa ANTES de levantarla. Arrancar primero y sembrar después dejaría el
# recuperador vacío hasta el siguiente reinicio.

smoke:  ## Explica el recibo de C-DEMO-01 y FALLA si la verificación no es PASS
	$(SMOKE) --api $(SMOKE_API) --cuenta $(CUENTA) --periodo $(PERIODO) --verbose

# --------------------------------------------------------------------------- #
# Evaluación, auditoría y calidad  (intérprete local)
# --------------------------------------------------------------------------- #
eval:  ## Ejecuta la evaluación oficial: recuperación, alucinación y hand-off
	$(PY) -m eval.run_eval --detalle $(ARGS)

golden:  ## Regenera los casos golden muestreados (hágalo después de cada `make seed`)
	$(PY) -m eval.generar_golden $(ARGS)

audit:  ## Verifica la cadena de hashes y muestra el último turno auditado
	@$(PY) -c "$$GUION_AUDITORIA"

test:  ## Ejecuta la batería completa de pruebas
	$(PY) -m pytest $(ARGS)

web-install:  ## Instala dependencias del workspace React
	npm install

web-dev:  ## Levanta Vite en :5173 (API en :8000)
	npm run dev:web

web-build:  ## Comprueba tipos y genera apps/web/dist
	npm run build:web

web-test:  ## Ejecuta pruebas unitarias del frontend
	npm run test:web

lint:  ## Comprueba estilo y formato sin modificar nada
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt:  ## Formatea y aplica las correcciones automáticas
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

# --------------------------------------------------------------------------- #
# Limpieza
# --------------------------------------------------------------------------- #
limpiar:  ## Borra contenedores, volúmenes y cachés de herramientas
	-$(COMPOSE) down -v --remove-orphans
	-rm -rf .pytest_cache .ruff_cache .hypothesis htmlcov .coverage
	-find . -type d -name '__pycache__' -prune -exec rm -rf {} +
	@echo "Listo. El dataset de data/ sigue ahí: use 'make limpiar-datos' para borrarlo."

limpiar-datos:  ## Borra el dataset sintético, la bitácora y la telemetría
	-rm -rf data/sintetico data/auditoria data/telemetria
	@echo "data/ vaciado. Regenérelo con 'make seed' (es determinístico: mismo seed, mismos bytes)."

# --------------------------------------------------------------------------- #
# Guiones auxiliares
# --------------------------------------------------------------------------- #
define GUION_AUDITORIA
import sys
from packages.governance.auditoria import formatear_para_terminal, registro_por_defecto

registro = registro_por_defecto()
trazas = registro.trazas()
print("bitacora :", registro.ruta)
print("turnos   :", len(trazas))
print()
if trazas:
    print(formatear_para_terminal(registro.leer(trazas[-1]), trazas[-1], color=False))
if not trazas:
    print("No hay turnos registrados todavia. Ejecute 'make smoke' y repita.")
valida, roto = registro.verificar_cadena()
print()
print("cadena_valida:", valida, "| indice_roto:", roto)
sys.exit(0 if valida else 1)
endef
export GUION_AUDITORIA
