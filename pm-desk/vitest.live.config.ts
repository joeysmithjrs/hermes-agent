import { defineConfig } from 'vitest/config';

/**
 * Opt-in live suites only. Never runs under `npm test`; invoked explicitly via
 * `npm run test:live`, which also sets PM_DESK_LIVE=1. Each live suite skips
 * itself if that variable is absent, so an accidental run is a no-op.
 */
export default defineConfig({
  test: {
    include: ['tests/live/**/*.test.ts'],
    environment: 'node',
    globals: false,
    testTimeout: 90_000,
    hookTimeout: 60_000,
  },
});
