# Multi-stage build. The engine itself has zero external crates (see the note at the top of
# Cargo.toml), so the builder stage only needs a Rust toolchain — no crates.io access required,
# on purpose, so this builds the same way this codebase was originally written: offline.

FROM rust:1-bookworm AS builder
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY examples ./examples
RUN cargo build --release

# Runtime stage. debian-slim, not scratch or distroless: this binary shells out to `openssl`
# (src/ed25519.rs — real, audited Ed25519 rather than a hand-rolled implementation) and to
# `curl` (src/liquidity.rs — the router's external price sources), so both need to be on PATH.
# The healthcheck deliberately does NOT use curl (see the comment at the top of main.rs) — it
# calls back into the binary itself with --healthcheck, so no extra package is needed for that.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        openssl \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --home-dir /app ikenga
WORKDIR /app
COPY --from=builder /build/target/release/ikenga-matching-engine /app/ikenga-matching-engine

# The WAL's default path (data/ikenga.wal, relative to the working directory) lives here so a
# volume mounted at /app/data is all that's needed for durability across container restarts —
# see docs/CUSTODY.md and src/wal.rs for why this file is what makes a restart survivable.
RUN mkdir -p /app/data && chown -R ikenga:ikenga /app
USER ikenga

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/app/ikenga-matching-engine", "--healthcheck"]

ENTRYPOINT ["/app/ikenga-matching-engine"]
