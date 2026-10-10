/** Readable message from a failed API call (FastAPI detail string, object, or validation list). */
export function apiErrorMessage(error: unknown, fallback: string): string {
  const err = error as {
    response?: { data?: { detail?: unknown; message?: unknown } }
    message?: string
  }
  const data = err?.response?.data
  const detail = data?.detail ?? data?.message

  const clean = (text: string) =>
    text
      .replace(/\u001b\[[0-9;]*[A-Za-z]/g, '')
      .replace(/\s+/g, ' ')
      .trim()

  if (typeof detail === 'string' && detail.trim()) {
    return clean(detail)
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object' && 'msg' in item) {
          const loc = Array.isArray((item as { loc?: unknown }).loc)
            ? (item as { loc: unknown[] }).loc.filter((p) => p !== 'body').join('.')
            : ''
          const msg = String((item as { msg: unknown }).msg)
          return loc ? `${loc}: ${msg}` : msg
        }
        return ''
      })
      .filter(Boolean)
    if (parts.length) return clean(parts.join('\n'))
  }
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const msg = String((detail as { message: unknown }).message || '')
    if (msg.trim()) return clean(msg)
  }
  if (err?.message && !/^Request failed with status code/.test(err.message)) {
    return clean(err.message)
  }
  return fallback
}
