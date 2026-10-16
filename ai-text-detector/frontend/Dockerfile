# syntax=docker/dockerfile:1
# Web tier: build the Angular SPA, then serve it (and reverse-proxy /api) via nginx.
#
# Build context is the REPO ROOT, so COPY paths are root-relative and the root
# .dockerignore keeps node_modules/dist/.angular out of the context (they are rebuilt
# inside the build stage below).

# ---- Stage 1: build the production bundle --------------------------------
FROM node:22-alpine AS build
WORKDIR /app

# Install from the lockfile first for a cached dependency layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Copy the project and build. The application builder emits to dist/frontend/browser.
COPY frontend/ ./
RUN npm run build -- --configuration production

# ---- Stage 2: serve static files + same-origin /api reverse proxy ----------
FROM nginx:1.27-alpine

# The SPA bundle.
COPY --from=build /app/dist/frontend/browser /usr/share/nginx/html
# Server block: SPA fallback + /api/ -> backend:8787/ (trailing slash strips /api).
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1
