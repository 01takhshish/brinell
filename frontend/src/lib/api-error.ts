// Shared by the server components in `api.ts` and by the login route handler, which talks to
// the same API but cannot go through `request` — it has no session yet, and a 401 there means
// "wrong password", not "go to the login page".

/**
 * Every error the API raises comes back in one envelope — `{error: {code, message, details}}` —
 * because its exception handlers wrap even FastAPI's own 422, whose default body is a bare list.
 * Reading `detail` here instead, as a FastAPI client normally would, turned a real validation
 * failure into the string "login failed" and cost an afternoon of looking in the wrong place.
 *
 * `details` carries the per-field messages on a 422; they are joined in because the field name is
 * usually the entire answer ("email: value is not a valid email address"), and a bare
 * "request body failed validation" is not something anyone can act on.
 */
export async function readDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as {
      error?: { message?: unknown; details?: unknown };
      detail?: unknown;
    };
    const message = body.error?.message ?? body.detail;
    const base = typeof message === "string" ? message : null;

    const details = body.error?.details;
    if (Array.isArray(details) && details.length > 0) {
      const fields = details
        .map((d: unknown) => {
          const { field, message: m } = (d ?? {}) as { field?: unknown; message?: unknown };
          if (typeof m !== "string") return null;
          return typeof field === "string" && field ? `${field}: ${m}` : m;
        })
        .filter((d): d is string => d !== null);
      if (fields.length > 0) return base ? `${base} (${fields.join("; ")})` : fields.join("; ");
    }

    return base ?? (response.statusText || "request failed");
  } catch {
    return response.statusText || "request failed";
  }
}
