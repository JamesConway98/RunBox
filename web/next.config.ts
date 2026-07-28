import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // No `rewrites()` here, on purpose. The API is a separate service, and calls
  // to it are proxied by the route handler at `src/app/api/[...path]/route.ts`
  // instead: a rewrite forwards the browser's request unchanged, so it has no
  // way to attach the Runbox API key that every endpoint requires.
  //
  // The browser still only ever talks to this origin, which was the original
  // reason for the rewrite — EventSource with no CORS preflight.
};

export default config;
