# ============================================================================
# フロント（Next.js）のイメージ。
#
# ★ サーバーとは別イメージにする。Python から Server Components や
#   Route Handler は配信できないので同居させられない。
#
# ★ ビルドコンテキストは web/ 。ルートの Dockerfile とは別物。
#
#     docker build -f web/Dockerfile -t calling-web ./web
#
# ★ API_BASE_URL は実行時に渡す。ビルド時に焼き込むと、環境ごとに
#   イメージを作り直すことになる（BFF はサーバー側で読むので実行時で足りる）。
# ============================================================================

FROM node:22-slim AS deps
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS builder
WORKDIR /build
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /build/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-slim AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000

WORKDIR /srv

# ★ standalone 出力を使う。node_modules を丸ごと運ぶとイメージが
#   数百 MB 膨らみ、デプロイのたびに転送することになる
COPY --from=builder /build/.next/standalone ./
COPY --from=builder /build/.next/static ./.next/static
COPY --from=builder /build/public ./public

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:'+(process.env.PORT||3000)+'/login',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

# ★ HOSTNAME を起動時に上書きする。
#
#   Next.js の standalone サーバーは HOSTNAME を bind アドレスとして使う。
#   一方 Docker は実行時に HOSTNAME へコンテナ ID を入れ、これは Dockerfile の
#   ENV より優先される。放っておくとコンテナ ID が解決する IP にだけ bind され、
#   127.0.0.1 から届かなくなる。
#
#   外部へのポート公開は効いてしまうので、「アプリは動いているのに
#   ヘルスチェックだけが落ちる」という気付きにくい壊れ方をする。
#   ENV では上書きできないので、コマンド側で指定する。
#
#   exec で置き換えるのは、SIGTERM をアプリに届けるため。
CMD ["sh", "-c", "HOSTNAME=0.0.0.0 exec node server.js"]
