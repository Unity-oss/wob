import type { NextConfig } from "next";

const apiUrl = process.env.API_URL ?? "http://127.0.0.1:8000";

const config: NextConfig = {
  experimental: {
    // Proxied request bodies are buffered up to this size (default 10MB). The upload panel
    // sends files in batches of at most UPLOAD_BATCH_BYTES (components/UploadPanel.tsx).
    proxyClientMaxBodySize: "120mb",
  },
  // Pages are never stored by the browser or a proxy, so after signing out nothing behind
  // sign-in can be shown again from a cache (hashed static assets keep their own caching).
  async headers() {
    return [
      {
        source: "/((?!_next/static|_next/image).*)",
        headers: [{ key: "Cache-Control", value: "no-store, max-age=0" }],
      },
    ];
  },
  // The browser only talks to this origin; API calls are proxied to FastAPI.
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default config;
