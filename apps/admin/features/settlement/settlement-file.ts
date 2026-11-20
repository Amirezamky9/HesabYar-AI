/**
 * Settlement file upload helpers (US-525) — the file travels to the
 * backend as base64 inside the JSON body (the BFF proxy parses JSON
 * only), like the bank imports. File bytes are never stored — only
 * the filename, SHA-256 hash and parsed rows.
 */

/** Decoded upload size cap (matches the backend's MAX_UPLOAD_BYTES). */
export const MAX_SETTLEMENT_FILE_BYTES = 10 * 1024 * 1024;

/** Row cap enforced by the backend parser. */
export const MAX_SETTLEMENT_IMPORT_ROWS = 2000;

export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("خواندن فایل ناموفق بود."));
        return;
      }
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(new Error("خواندن فایل ناموفق بود."));
    reader.readAsDataURL(file);
  });
}
