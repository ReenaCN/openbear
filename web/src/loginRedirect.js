// Only notification destinations may survive authentication. Never accept an
// arbitrary return URL, including protocol-relative URLs or backslash aliases.
export function notificationTarget(value, origin) {
  if (typeof value !== "string" || !value || /[\\\u0000-\u0020\u007f]/.test(value)) return "";
  try {
    const url = new URL(value, origin);
    if (url.origin !== origin || url.username || url.password || !["/chat", "/settings", "/recommendations"].includes(url.pathname)) return "";
    return url.pathname + url.search;
  } catch { return ""; }
}

function returnTarget(value, origin) {
  if (!value?.startsWith("/") || value.startsWith("//")) return "";
  return notificationTarget(value, origin);
}

export function loginUrlForLocation(location) {
  const target = returnTarget(location.pathname + location.search, location.origin);
  return target ? `/login?next=${encodeURIComponent(target)}` : "/login";
}

export function loginSuccessTarget(location) {
  return returnTarget(new URLSearchParams(location.search).get("next"), location.origin) || "/";
}
