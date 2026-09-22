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
  n >= 1e9
    ? `${(n / 1e9).toFixed(2)} GB`
    : n >= 1e6
      ? `${(n / 1e6).toFixed(1)} MB`
      : n >= 1e3
        ? `${(n / 1e3).toFixed(1)} KB`
        : `${n} B`;
export const clock = (seconds: number) =>
  new Date(Math.max(0, seconds) * 1000).toISOString().slice(11, 19);

// Send the file directly; a separate JSON API would buffer or base64-expand it.
export function uploadSubtitle(
  jobId: string,
  file: File,
  code: string,
  hearingImpaired: boolean,
  onProgress: (value: number | null) => void,
): Promise<{ duplicate: boolean; queued?: boolean }> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const params = new URLSearchParams({
      filename: file.name,
      code,
      hearing_impaired: String(hearingImpaired),
    });
    request.open("POST", `/api/jobs/${jobId}/tracks/upload?${params}`);
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.timeout = 10 * 60 * 1000;
    request.upload.onprogress = (event) =>
      onProgress(
        event.lengthComputable ? (event.loaded / event.total) * 100 : null,
      );
    request.onerror = request.ontimeout = () =>
      reject(
        new ApiError(
          "Upload interrupted. You can retry the same file safely.",
          null,
          true,
        ),
      );
    request.onload = () => {
      let result: { detail?: string; duplicate: boolean };
      try {
        result = JSON.parse(request.responseText);
      } catch {
        result = { duplicate: false };
      }
      if (request.status >= 200 && request.status < 300) resolve(result);
      else {
        if (request.status === 401)
          window.dispatchEvent(new Event("session-expired"));
        reject(
          new ApiError(
            typeof result.detail === "string"
              ? result.detail
              : `Upload failed (${request.status}).`,
            request.status,
          ),
        );
      }
    };
    request.send(file);
  });
}
