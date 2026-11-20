FROM node:24-bookworm-slim@sha256:dc26ae92c9fb15cf6b1d63e20f0944b2efe0f115b20e00be48fb23fceb061c54

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates coreutils git python3 python-is-python3 squid \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/actualize-evals
COPY package.json package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm ci --include=dev --no-audit --fund=false --registry="${NPM_REGISTRY}" \
    --fetch-retries=1 --fetch-retry-mintimeout=1000 --fetch-retry-maxtimeout=5000 --fetch-timeout=60000 \
    && npm cache clean --force

COPY evals/docker/squid.conf /etc/squid/squid.conf
ENV PATH="/opt/actualize-evals/node_modules/.bin:${PATH}" \
    VALLY_TELEMETRY_OPTOUT=1 \
    NODE_USE_ENV_PROXY=1 \
    PYTHONDONTWRITEBYTECODE=1
USER node
WORKDIR /home/node
CMD ["sh"]
