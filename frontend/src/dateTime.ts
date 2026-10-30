const CAT_TIME_ZONE = 'Africa/Harare'

export function formatCatDateTime(value?: string | number | null, compact = false): string {
  if (value === undefined || value === null || value === '') return 'Not available'
  const numericValue = typeof value === 'number' || /^\d{10,13}$/.test(value) ? Number(value) : NaN
  const date = new Date(Number.isFinite(numericValue) ? numericValue : value)
  if (Number.isNaN(date.getTime())) return 'Not available'

  const dateLabel = new Intl.DateTimeFormat('en-GB', {
    timeZone: CAT_TIME_ZONE,
    day: 'numeric',
    month: 'short',
    ...(compact ? {} : { year: 'numeric' as const }),
  }).format(date)
  const timeLabel = new Intl.DateTimeFormat('en-GB', {
    timeZone: CAT_TIME_ZONE,
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(date)
  return `${dateLabel}, ${timeLabel} CAT`
}

export function formatEmailDate(message: { internal_date?: string | number | null; date?: string | null }, compact = false): string {
  const timestamp = message.internal_date || message.date
  return formatCatDateTime(timestamp, compact)
}
