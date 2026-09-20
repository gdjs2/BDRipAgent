export class ApiError extends Error {
  status: number | null;
  retryable: boolean;

  constructor(message: string, status: number | null, retryable = false) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.retryable = retryable;
  }
}

export const isConnectionError = (error: unknown) =>
  error instanceof ApiError && error.retryable;

export async function api<T>(
  path: string,
  body?: unknown,
  method?: string,
): Promise<T> {
  const verb = (method ?? (body === undefined ? "GET" : "POST")).toUpperCase();
  const payload = body === undefined ? undefined : JSON.stringify(body);
  // Reads can recover automatically; repeating an unconfirmed action could
  // create duplicate jobs or apply a change twice.
  const attempts = verb === "GET" ? 3 : 1;
  for (let attempt = 0; attempt < attempts; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60000);
    let failure: ApiError;
    try {
      const response = await fetch("/api" + path, {
        method: verb,
        credentials: "same-origin",
        headers:
          body === undefined ? {} : { "Content-Type": "application/json" },
        body: payload,
        signal: controller.signal,
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        if (response.status === 401 && path !== "/session")
          window.dispatchEvent(new Event("session-expired"));
        const temporary = [502, 503, 504].includes(response.status);
        throw new ApiError(
          typeof error?.detail === "string"
            ? error.detail
            : error?.detail
              ? JSON.stringify(error.detail)
              : temporary
                ? "The server is temporarily unavailable. Please try again shortly."
                : response.statusText || `Request failed (${response.status}).`,
          response.status,
          temporary,
        );
      }
      return response.status === 204 ? (undefined as T) : await response.json();
    } catch (error) {
      failure =
        error instanceof ApiError
          ? error
          : new ApiError(
              verb !== "GET"
                ? "Connection lost before the action was confirmed. Refresh to check whether it completed before trying again."
                : controller.signal.aborted
                  ? "The server took too long to respond. Please try again."
                  : "Cannot reach the server. Check your connection and try again.",
              null,
              true,
            );
    } finally {
      clearTimeout(timer);
    }
    if (!failure.retryable || attempt === attempts - 1) throw failure;
    await new Promise((resolve) => setTimeout(resolve, 500 * 2 ** attempt));
  }
  throw new Error("Request attempts exhausted");
}
export const readable = (value: string) =>
  value.toLowerCase().replaceAll("_", " ");
export const size = (n: number) =>
  n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`;
export const clock = (seconds: number) =>
  new Date(Math.max(0, seconds) * 1000).toISOString().slice(11, 19);
