/**
 * The registry of research tools this desk has already shipped.
 *
 * This exists because every shipped tool re-creates the same awareness gap it
 * was built to close: a morning that does not know `cpi-calibrate` exists will
 * "propose building" a CPI nowcast harness, and a reopen that does not know it
 * exists will gate Joe on coding that should already be done. The registry is
 * the single source of truth the plan schema, the prompts, and the taxonomy
 * consult, so a shipped tool cannot be re-proposed and an agent always knows
 * what it has.
 *
 * Paper only. Nothing here trades; the registry is metadata, not a callable.
 */

/** A research tool that has shipped and must not be re-proposed as a buildout. */
export interface ShippedResearchTool {
  /** The id a `proposed_buildouts[].id` is forbidden from reusing. */
  id: string;
  /** The CLI an agent runs instead of proposing to build this. */
  cli: string;
  /** The desk capability this tool provides. */
  capability: string;
  status: 'shipped';
}

/**
 * The shipped set. Add a row here when a research harness lands on main; the
 * schema ban, the prompt awareness, and the capability detection all read from
 * this list, so "the desk forgot it had this" stops being a possible failure.
 */
export const SHIPPED_RESEARCH_TOOLS = [
  {
    id: 'cpi_nowcast_bucket_harness',
    cli: 'pm-desk research cpi-calibrate',
    capability: 'cpi_nowcast_calibration',
    status: 'shipped',
  },
] as const satisfies readonly ShippedResearchTool[];

/** Every shipped tool id, for the schema's banned-buildout refine. */
export const SHIPPED_TOOL_IDS: readonly string[] = SHIPPED_RESEARCH_TOOLS.map((t) => t.id);

/**
 * The set form, for `proposed_buildouts[].id` membership checks. Same data as
 * {@link SHIPPED_TOOL_IDS}; a `Set` is the shape the refine reads fastest.
 */
export const BANNED_BUILDOUT_IDS: ReadonlySet<string> = new Set(SHIPPED_TOOL_IDS);

/** Is `id` a tool that has already shipped (and so may not be re-proposed)? */
export function isShippedToolId(id: string): boolean {
  return BANNED_BUILDOUT_IDS.has(id);
}

/** The capabilities a shipped tool provides, for `detectCapabilities()`. */
export function shippedResearchCapabilities(): readonly string[] {
  return SHIPPED_RESEARCH_TOOLS.map((t) => t.capability);
}

/** Human-readable list for prompts and `--help` surfaces. */
export function shippedToolLines(): string[] {
  return SHIPPED_RESEARCH_TOOLS.map(
    (t) => `- ${t.id} → \`${t.cli}\` (capability: ${t.capability})`,
  );
}
