// Optional Cloudflare Worker. Bind only the download routes, not the whole site.
// RELEASE_MANIFEST is an operator-supplied JSON string containing actual uploaded URLs.
const platforms = {
  windows: { key: "windows-x64", label: "Windows (64-bit)", signing: "signed" },
  macos: { key: "macos-universal2", label: "macOS (Intel and Apple Silicon)", signing: "notarized" },
  linux: { key: "linux-x64", label: "Linux (64-bit x86)", signing: null },
};

export function detectOS(headers) {
  const ua = headers.get("user-agent") || "";
  const hint = (headers.get("sec-ch-ua-platform") || "").replaceAll('"', "");
  if (/Android|iPhone|iPad|iPod|Mobile/i.test(ua)) return null;
  if (hint === "Windows" || /Windows NT/i.test(ua)) return "windows";
  if (hint === "macOS" || /Macintosh|Mac OS X/i.test(ua)) return "macos";
  if (hint === "Linux" || /Linux/i.test(ua)) {
    // Do not hand an x86 binary to a detected ARM machine.
    if (/aarch64|arm64|armv\d/i.test(ua)) return null;
    return "linux";
  }
  return null;
}

function page(status, title, message) {
  const links = Object.entries(platforms).map(([os, info]) =>
    `<li><a href="?os=${os}">${info.label}</a></li>`).join("");
  return new Response(`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>CometAPI Connect</title><body><main><h1>${title}</h1><p>${message}</p><ul>${links}</ul><p>Linux: extract the archive, then open CometAPI Connect. Your file manager may require enabling “Allow executing as a program.”</p></main></body></html>`, {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
      "Content-Security-Policy": "default-src 'none'; base-uri 'none'; frame-ancestors 'none'",
      "X-Content-Type-Options": "nosniff" },
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
    }
    const url = new URL(request.url);
    const segment = url.pathname.replace(/\/$/, "").split("/").pop();
    const explicit = url.searchParams.get("os") || (platforms[segment] ? segment : null);
    if (explicit && !Object.hasOwn(platforms, explicit)) return page(400, "Choose your download", "That operating system is not supported by this release.");
    const os = explicit || detectOS(request.headers);
    if (!os) return page(200, "Download CometAPI Connect", "Choose the computer where you will run the app. Mobile devices are not supported.");
    let asset;
    try {
      const manifest = JSON.parse(env.RELEASE_MANIFEST || "{}");
      asset = manifest.downloads?.[platforms[os].key];
      const destination = new URL(asset?.url);
      if (destination.protocol !== "https:" || destination.username || destination.password || destination.href === url.href) throw new Error();
      if (!/^[a-f0-9]{64}$/i.test(asset.sha256) || asset.verified !== true) throw new Error();
      if (platforms[os].signing && asset.signing !== platforms[os].signing) throw new Error();
    } catch {
      return page(503, "Download not available yet", "The verified download for this operating system has not been published. Please check back after the release is available.");
    }
    return new Response(null, { status: 302, headers: {
      Location: asset.url, "Cache-Control": "no-store", Vary: "User-Agent, Sec-CH-UA-Platform",
      "X-Content-Type-Options": "nosniff", "X-Artifact-SHA256": asset.sha256,
    } });
  },
};
