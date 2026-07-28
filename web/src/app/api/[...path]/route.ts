import { NextRequest } from "next/server";

/**
 * The API proxy.
 *
 * Everything the browser calls goes to `/api/*` on this origin and is forwarded
 * from here to the control plane, with the tenant's Runbox key attached
 * server-side.
 *
 * This exists because a `next.config` rewrite cannot do the one thing that
 * matters: a rewrite forwards the request it was given, and the browser has no
 * `Authorization` header to give. The dashboard talked to the API for weeks
 * with no credential at all, which the control plane answered — correctly —
 * with "Provide an API key as 'Authorization: Bearer rb_live_...'".
 *
 * The key lives in `RUNBOX_API_KEY`, with no `NEXT_PUBLIC_` prefix, so it is
 * readable only in this process. A visitor can use the dashboard, and cannot
 * read the key out of it — which is the whole point of proxying rather than
 * shipping a key to the client and calling the API directly.
 *
 * Note what is *not* proxied in: the visitor's own model provider key. That
 * travels on the request as `X-Provider-Key`, is passed straight through, and
 * is never stored here or read by this handler.
 */

// SSE, and per-visitor auth. Nothing about this may be cached or prerendered.
export const dynamic = "force-dynamic";
// Explicit: streaming a response body through requires the Node runtime.
export const runtime = "nodejs";

const UPSTREAM = process.env.RUNBOX_API_URL ?? "http://localhost:8000";
const API_KEY = process.env.RUNBOX_API_KEY ?? "";

/**
 * Headers copied from the browser's request.
 *
 * An allowlist rather than a denylist. Forwarding everything would send cookies
 * and the browser's own `Authorization` (if a future feature ever sets one)
 * upstream, and the point of this handler is to control exactly one credential.
 *
 * `last-event-id` is here because it is how an SSE reconnect says where it got
 * to. Drop it and every reconnect replays the trace from zero.
 */
const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "content-type",
  "last-event-id",
  "x-provider-key",
] as const;

/**
 * Headers copied back from upstream.
 *
 * `content-length` and `content-encoding` are deliberately absent: the body is
 * re-streamed, so upstream's numbers describe a body that no longer exists in
 * that form, and a wrong `content-length` truncates the response.
 */
const FORWARDED_RESPONSE_HEADERS = [
  "content-type",
  "cache-control",
  "retry-after",
  "www-authenticate",
] as const;

function envelope(status: number, error: string, message: string): Response {
  return new Response(JSON.stringify({ error, message }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function proxy(request: NextRequest, path: string[]): Promise<Response> {
  if (!API_KEY) {
    // A deployment problem, said plainly. Without this the UI would surface the
    // control plane's 401 and send the reader looking for a key they cannot
    // set, when the real fix is one environment variable on the server.
    return envelope(
      500,
      "server_misconfigured",
      "RUNBOX_API_KEY is not set on the dashboard. The server cannot authenticate to the control plane.",
    );
  }

  const url = new URL(`${UPSTREAM}/${path.join("/")}`);
  url.search = request.nextUrl.search;

  const headers = new Headers({ authorization: `Bearer ${API_KEY}` });
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  // Bodies here are small JSON documents, so buffering one is cheaper than the
  // duplex streaming dance and works on every runtime.
  const body =
    request.method === "GET" || request.method === "HEAD" ? undefined : await request.text();

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: request.method,
      headers,
      body,
      // The caller aborting — closing a pane mid-run — should hang up the
      // upstream connection too, not leave it streaming into nothing.
      signal: request.signal,
      cache: "no-store",
      redirect: "manual",
    });
  } catch (error) {
    if (request.signal.aborted) {
      // The client left. Nobody is waiting for this.
      return new Response(null, { status: 499 });
    }
    return envelope(
      502,
      "upstream_unreachable",
      `Could not reach the control plane: ${error instanceof Error ? error.message : "unknown error"}`,
    );
  }

  const responseHeaders = new Headers();
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  // Nginx and friends buffer proxied responses by default, which for SSE means
  // the trace arrives in one lump when the run ends — technically correct and
  // completely useless to watch.
  responseHeaders.set("x-accel-buffering", "no");

  // `upstream.body` is passed through rather than awaited, so an SSE stream
  // reaches the browser token by token instead of at the end.
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path);
}

export async function POST(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path);
}

export async function DELETE(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path);
}

export async function PATCH(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path);
}

export async function PUT(request: NextRequest, context: Context): Promise<Response> {
  return proxy(request, (await context.params).path);
}
