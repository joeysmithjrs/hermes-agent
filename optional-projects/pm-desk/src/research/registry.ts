
export const SHIPPED_RESEARCH_TOOLS = [
  {
    id: 'cpi_nowcast_bucket_harness',
    cli: 'pm-desk research cpi-calibrate',
    capability: 'cpi_nowcast_calibration',
    status: 'shipped',
  },
] as const

export const BANNED_BUILDOUT_IDS = new Set([
  'cpi_nowcast_bucket_harness',
  // Do not re-propose already-shipped harnesses
])
