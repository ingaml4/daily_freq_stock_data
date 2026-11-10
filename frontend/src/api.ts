export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function safeNext(value: unknown): string {
  const text = typeof value === "string" ? value : "";
  if (
    !text.startsWith("/") ||
    text.startsWith("//") ||
    text.includes("\\") ||
    /[\u0000-\u001f]/.test(text)
  ) {
    return "/signals/vol_spike";
  }
  try {
    const target = new URL(text, window.location.origin);
    return target.origin === window.location.origin &&
      target.pathname !== "/login"
      ? target.pathname + target.search + target.hash
      : "/signals/vol_spike";
  } catch {
    return "/signals/vol_spike";
  }
}

export async function requestJson<T>(
  path: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    signal,
    headers: { Accept: "application/json" },
  });
  if (response.status === 401 && window.location.pathname !== "/login") {
    const next = window.location.pathname + window.location.search;
    window.location.assign("/login?next=" + encodeURIComponent(next));
    throw new ApiError("登录已过期，请重新登录", 401);
  }
  if (!response.ok) {
    const data = (await response.json().catch(() => null)) as {
      error?: string;
    } | null;
    throw new ApiError(
      data?.error || `请求失败（${response.status}）`,
      response.status,
    );
  }
  return response.json() as Promise<T>;
}

export function getJson<T>(
  path: string,
  params?: Record<string, string | number | undefined>,
  signal?: AbortSignal,
): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  return requestJson<T>(
    path + (query.size ? "?" + query.toString() : ""),
    signal,
  );
}
