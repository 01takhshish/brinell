import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `standalone` makes `next build` emit a self-contained server under .next/standalone — the
  // Dockerfile copies that instead of node_modules, which is the difference between a ~150MB
  // image and a ~1GB one.
  output: "standalone",
};

export default nextConfig;
