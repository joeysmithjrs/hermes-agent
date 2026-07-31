/**
 * Types for `preflight.mjs`.
 *
 * The script stays dependency-free plain JS on purpose: it must be runnable on
 * a checkout whose `npm install` was refused by `engine-strict`, i.e. before
 * `tsx` exists. This declaration lets the test suite import its pure functions
 * with types anyway.
 */

export declare function parseRequiredMajor(range: unknown): number | null;

export declare function evaluate(
  nodeVersion: string,
  range: unknown,
): { ok: boolean; requiredMajor: number | null; actualMajor: number };

export declare function failureMessage(nodeVersion: string, requiredMajor: number): string;
