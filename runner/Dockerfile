# Multi-stage: a full toolchain to build, almost nothing to run.

FROM golang:1.24-bookworm AS build

WORKDIR /src

# Dependencies first, so a source-only change does not re-download the module
# graph.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO off produces a static binary, which is what makes the distroless base
# below viable. -trimpath keeps build-host paths out of stack traces.
RUN CGO_ENABLED=0 GOOS=linux go build \
      -trimpath \
      -ldflags="-s -w" \
      -o /out/runner ./cmd/runner

FROM gcr.io/distroless/static-debian12:nonroot

# The runner needs the host's Docker socket mounted at run time. That is the
# one genuinely privileged thing about this deployment and it is worth being
# explicit about rather than burying in a compose file: whoever can reach that
# socket is root on the host.
COPY --from=build /out/runner /usr/local/bin/runner

USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/runner"]
