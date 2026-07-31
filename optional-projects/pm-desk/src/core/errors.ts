/**
 * Typed error hierarchy. Every failure the desk can surface to an operator gets a
 * machine-readable `code` and, wherever we can say something useful, a `hint` that
 * names the concrete next action. CLI entry points render these via `toCliString()`.
 */

export type ErrorCode =
  | 'CONFIG_ERROR'
  | 'SCHEMA_VALIDATION_ERROR'
  | 'STORE_ERROR'
  | 'SOURCE_SPEC_ERROR'
  | 'SOURCE_POLICY_ERROR'
  | 'COLLECTION_ERROR'
  | 'MARKET_DATA_ERROR'
  | 'CAPABILITY_UNAVAILABLE'
  | 'MONITOR_SPEC_ERROR'
  | 'INGRESS_AUTH_ERROR'
  | 'DISPATCH_DISABLED'
  | 'DISPATCH_ERROR'
  | 'LEDGER_INVARIANT_ERROR'
  | 'TAXONOMY_ERROR'
  | 'USAGE_ERROR';

export interface PmDeskErrorOptions {
  hint?: string;
  details?: Record<string, unknown>;
  cause?: unknown;
}

export class PmDeskError extends Error {
  readonly code: ErrorCode;
  readonly hint?: string;
  readonly details?: Record<string, unknown>;

  constructor(code: ErrorCode, message: string, options: PmDeskErrorOptions = {}) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = new.target.name;
    this.code = code;
    this.hint = options.hint;
    this.details = options.details;
  }

  /** Single actionable block for terminal output. */
  toCliString(): string {
    const lines = [`${this.code}: ${this.message}`];
    if (this.hint) lines.push(`  hint: ${this.hint}`);
    return lines.join('\n');
  }

  toJSON(): Record<string, unknown> {
    return {
      code: this.code,
      message: this.message,
      ...(this.hint ? { hint: this.hint } : {}),
      ...(this.details ? { details: this.details } : {}),
    };
  }
}

function subclass(code: ErrorCode) {
  return class extends PmDeskError {
    constructor(message: string, options: PmDeskErrorOptions = {}) {
      super(code, message, options);
    }
  };
}

export const ConfigError = subclass('CONFIG_ERROR');
export const SchemaValidationError = subclass('SCHEMA_VALIDATION_ERROR');
export const StoreError = subclass('STORE_ERROR');
export const SourceSpecError = subclass('SOURCE_SPEC_ERROR');
export const SourcePolicyError = subclass('SOURCE_POLICY_ERROR');
export const CollectionError = subclass('COLLECTION_ERROR');
export const MarketDataError = subclass('MARKET_DATA_ERROR');
export const CapabilityUnavailableError = subclass('CAPABILITY_UNAVAILABLE');
export const MonitorSpecError = subclass('MONITOR_SPEC_ERROR');
export const IngressAuthError = subclass('INGRESS_AUTH_ERROR');
export const DispatchDisabledError = subclass('DISPATCH_DISABLED');
export const DispatchError = subclass('DISPATCH_ERROR');
export const LedgerInvariantError = subclass('LEDGER_INVARIANT_ERROR');
export const TaxonomyError = subclass('TAXONOMY_ERROR');
export const UsageError = subclass('USAGE_ERROR');

// The subclasses are const class expressions (values). These aliases let call
// sites and tests use the same identifier in type position.
export type ConfigError = InstanceType<typeof ConfigError>;
export type SchemaValidationError = InstanceType<typeof SchemaValidationError>;
export type StoreError = InstanceType<typeof StoreError>;
export type SourceSpecError = InstanceType<typeof SourceSpecError>;
export type SourcePolicyError = InstanceType<typeof SourcePolicyError>;
export type CollectionError = InstanceType<typeof CollectionError>;
export type MarketDataError = InstanceType<typeof MarketDataError>;
export type CapabilityUnavailableError = InstanceType<typeof CapabilityUnavailableError>;
export type MonitorSpecError = InstanceType<typeof MonitorSpecError>;
export type IngressAuthError = InstanceType<typeof IngressAuthError>;
export type DispatchDisabledError = InstanceType<typeof DispatchDisabledError>;
export type DispatchError = InstanceType<typeof DispatchError>;
export type LedgerInvariantError = InstanceType<typeof LedgerInvariantError>;
export type TaxonomyError = InstanceType<typeof TaxonomyError>;
export type UsageError = InstanceType<typeof UsageError>;

export function isPmDeskError(value: unknown): value is PmDeskError {
  return value instanceof PmDeskError;
}
