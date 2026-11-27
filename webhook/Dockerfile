FROM golang:1.24-bookworm AS webhook
WORKDIR /src
COPY . .
RUN go build -trimpath -o /webhook .

FROM rust:1.100-bookworm AS reviewer
RUN apt-get update && apt-get install -y --no-install-recommends cmake && rm -rf /var/lib/apt/lists/*
ARG WREV_REVISION=157641582a542e9ef5ff73cff575cf58f83ff017
RUN echo "$WREV_REVISION" | grep -Eq '^[0-9a-f]{40}$' && \
    git init /wrev && git -C /wrev fetch --depth 1 https://github.com/r33drichards/wrev.git "$WREV_REVISION" && \
    git -C /wrev checkout --detach FETCH_HEAD && test "$(git -C /wrev rev-parse HEAD)" = "$WREV_REVISION"
WORKDIR /wrev
RUN cargo build --locked --release --features sqlite-backend --bin wrev

FROM debian:bookworm-slim
ARG WREV_REVISION=157641582a542e9ef5ff73cff575cf58f83ff017
LABEL io.wrev.revision=$WREV_REVISION
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=webhook /webhook /usr/local/bin/webhook
COPY --from=reviewer /wrev/target/release/wrev /usr/local/bin/wrev
ENTRYPOINT ["sh", "-c", "umask 077; exec webhook -port \"${PORT:-8080}\" -wrev-binary /usr/local/bin/wrev"]
