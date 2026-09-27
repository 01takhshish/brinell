# The dashboard. It is a BFF — the browser only ever talks to this server, and this server talks
# to the API over the internal network (CRUCIBLE_API_URL), forwarding the session cookie. That is
# why there is no NEXT_PUBLIC_* anything here: no API address is ever baked into client bundles,
# so one image works in every environment.

FROM node:22-alpine AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build


FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /app

# `output: "standalone"` in next.config.ts is what makes these three copies a complete server.
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

USER node
EXPOSE 3200
ENV PORT=3200 HOSTNAME=0.0.0.0

CMD ["node", "server.js"]
