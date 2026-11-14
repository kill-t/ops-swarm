FROM node:24.20.0-alpine AS builder

WORKDIR /app

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.29.8-alpine

ARG OPSSWARM_BUILD_REVISION=unknown
LABEL org.opencontainers.image.revision=$OPSSWARM_BUILD_REVISION

COPY docker/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080
