import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The API is a separate service. Proxying it through Next in development
  // keeps the browser on one origin, so EventSource works without CORS
  // preflight games and cookies would behave if we ever added them.
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${process.env.RUNBOX_API_URL ?? "http://localhost:8000"}/:path*`,
      },
    ];
  },
};

export default config;
