/**
 * Statement file helpers (US-514, B1).
 *
 * The import API takes the file as base64 JSON (the BFF proxy parses
 * bodies as JSON only — no multipart). The backend enforces the same
 * 10 MB cap; the UI checks first so a too-large file fails fast with
 * a Persian message instead of a round trip.
 */

export const MAX_IMPORT_FILE_BYTES = 10 * 1024 * 1024;

/** Bank-export row cap, enforced by the backend (phased design §4). */
export const MAX_IMPORT_ROWS = 2000;

/** Read a File as a base64 string (no data-URL prefix). */
export async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  // btoa takes a binary string; chunk so large files do not blow the stack.
  const CHUNK = 0x8000;
  let binary = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/**
 * A column mapping is complete when it has a date and a description and
 * either a signed amount or a debit+credit pair (same rule as the
 * backend's validate_mapping_columns).
 */
export function mappingIsComplete(columns: Record<string, string>): boolean {
  if (!columns.date || !columns.description) return false;
  return Boolean(columns.amount || (columns.debit && columns.credit));
}
