/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The API lives on a different origin (Render), so requests go straight
  // there with a bearer token rather than through a Next.js proxy. That keeps
  // the frontend a static export target and avoids a second hop.
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL,
  },
};

export default nextConfig;
