# Frontend React y arquitectura monorepo

El repositorio es un monorepo poliglota: Python administra el backend con
`pyproject.toml` y npm workspaces administra React desde el `package.json` raiz.

```text
apps/api/              FastAPI
apps/web/src/          React + TypeScript
apps/web/estatico/     consola anterior, conservada como fallback
apps/web/dist/         build generado (no se versiona)
packages/              dominio, hechos, RAG, IA y gobernanza en Python
```

React solo consume contratos HTTP. No calcula importes ni recibe claves de modelos.
Vite reenvia `/salud`, `/dev` y `/v1` a la API local. En Docker, una etapa Node
compila React y copia `dist` dentro de la imagen final de FastAPI. La SPA queda bajo
`/ui/`; no existe un segundo contenedor web.

## Desarrollo local

Requisitos: Python 3.12 y Node 20 o posterior.

```bash
python -m pip install -r requirements-dev.txt
npm install
```

En dos terminales:

```bash
# Terminal 1
python scripts/dev.py

# Terminal 2
npm run dev:web
```

Abrir `http://localhost:5173/ui/`.

## Comprobaciones

```bash
npm run typecheck:web
npm run test:web
npm run build:web
python -m pytest
```

Despues del build, reiniciar FastAPI y abrir `http://127.0.0.1:8000/ui/`.

## Docker

```bash
docker compose up --build
```

Abrir `http://localhost:8000/ui/`. React, la API y Swagger comparten un contenedor,
puerto y dominio. Swagger queda en `http://localhost:8000/docs`.
