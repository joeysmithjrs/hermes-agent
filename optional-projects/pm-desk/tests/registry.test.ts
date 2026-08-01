import { describe, expect, it } from 'vitest';

import {
  BANNED_BUILDOUT_IDS,
  SHIPPED_RESEARCH_TOOLS,
  SHIPPED_TOOL_IDS,
  isShippedToolId,
  shippedResearchCapabilities,
  shippedToolLines,
} from '../src/research/registry.js';

describe('shipped research registry', () => {
  it('lists the CPI nowcast harness as shipped', () => {
    expect(SHIPPED_RESEARCH_TOOLS.map((t) => t.id)).toContain('cpi_nowcast_bucket_harness');
    const harness = SHIPPED_RESEARCH_TOOLS.find((t) => t.id === 'cpi_nowcast_bucket_harness');
    expect(harness?.cli).toBe('pm-desk research cpi-calibrate');
    expect(harness?.status).toBe('shipped');
  });

  it('flags shipped ids so the schema refine can ban them', () => {
    expect(isShippedToolId('cpi_nowcast_bucket_harness')).toBe(true);
    expect(isShippedToolId('something_not_shipped')).toBe(false);
    expect(BANNED_BUILDOUT_IDS.has('cpi_nowcast_bucket_harness')).toBe(true);
    expect(SHIPPED_TOOL_IDS).toContain('cpi_nowcast_bucket_harness');
  });

  it('exposes the capabilities a shipped tool provides', () => {
    expect(shippedResearchCapabilities()).toContain('cpi_nowcast_calibration');
  });

  it('renders a human-readable line per tool for prompts', () => {
    const lines = shippedToolLines();
    expect(lines.length).toBe(SHIPPED_RESEARCH_TOOLS.length);
    expect(lines[0]).toContain('cpi_nowcast_bucket_harness');
    expect(lines[0]).toContain('pm-desk research cpi-calibrate');
  });
});
