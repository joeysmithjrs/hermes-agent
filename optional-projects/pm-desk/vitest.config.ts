import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    // Live integration suites are opt-in: they are excluded from the default run
    // and only executed via `npm run test:live` with PM_DESK_LIVE=1.
    exclude: ['node_modules/**', 'dist/**', 'tests/live/**'],
    environment: 'node',
    globals: false,
    testTimeout: 20_000,
  },
});
