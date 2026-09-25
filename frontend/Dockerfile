FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build
FROM nginx:1.28-alpine
COPY frontend/nginx-main.conf /etc/nginx/nginx.conf
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

ARG APP_UID=1000
ARG APP_GID=1000
RUN test "$APP_UID" -gt 0 && test "$APP_GID" -gt 0 && \
    addgroup -g "$APP_GID" app && adduser -D -u "$APP_UID" -G app app
USER app
ENTRYPOINT ["nginx", "-g", "daemon off;"]
