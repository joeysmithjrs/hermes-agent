import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * Structural enforcement of the paper-only boundary.
 *
 * This desk must not be able to trade, sign, or touch a wallet — not "does not
 * today", but cannot without a test failing. Comments are stripped before
 * scanning so that documentation of the boundary (which necessarily names the
 * forbidden symbols) does not itself trip the guard; executable code cannot hide
 * in a comment.
 */

const SRC = join(import.meta.dirname, '..', 'src');

function listTsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...listTsFiles(full));
    else if (entry.endsWith('.ts')) out.push(full);
  }
  return out;
}

function stripComments(source: string): string {
  return source.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');
}

/** Symbols whose presence in executable code would mean the desk can act. */
const PROHIBITED_SYMBOLS = [
  'createSecureClient',
  'SecureClient',
  'secureClient',
  'tradingActions',
  'walletActions',
  'rfqActions',
  'perpsActions',
  'postOrder',
  'postOrders',
  'placeLimitOrder',
  'placeMarketOrder',
  'placeOrder',
  'createLimitOrder',
  'createMarketOrder',
  'cancelOrder',
  'cancelAll',
  'prepareLimitOrder',
  'prepareMarketOrder',
  'setupTradingApprovals',
  'approveErc20',
  'approveErc1155ForAll',
  'transferErc20',
  'splitPosition',
  'mergePositions',
  'redeemPositions',
  'depositToPerps',
  'withdrawFromPerps',
  'openPerpsSession',
  'beginAuthentication',
  'endAuthentication',
  'signTypedData',
  'privateKey',
  'PRIVATE_KEY',
  'MNEMONIC',
  'mnemonic',
  'walletConnect',
  'umaPropose',
  'umaDispute',
];

/** Packages that only exist to sign or move funds. */
const PROHIBITED_IMPORTS = [
  '@polymarket/client/actions',
  '@polymarket/client/viem',
  '@polymarket/client/ethers-v5',
  '@polymarket/client/privy',
  'ethers',
  'viem',
  'ox',
  '@privy-io',
  'web3',
];

const sourceFiles = listTsFiles(SRC);

describe('paper-only guard', () => {
  it('finds source files to scan (guard is not vacuously passing)', () => {
    expect(sourceFiles.length).toBeGreaterThan(10);
  });

  it('no production module references a trading, signing or wallet symbol', () => {
    const violations: string[] = [];
    for (const file of sourceFiles) {
      const code = stripComments(readFileSync(file, 'utf8'));
      for (const symbol of PROHIBITED_SYMBOLS) {
        const pattern = new RegExp(`\\b${symbol}\\b`);
        if (pattern.test(code)) {
          violations.push(`${relative(SRC, file)}: ${symbol}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('no production module imports a signing or wallet package', () => {
    const violations: string[] = [];
    for (const file of sourceFiles) {
      const code = stripComments(readFileSync(file, 'utf8'));
      for (const pkg of PROHIBITED_IMPORTS) {
        const pattern = new RegExp(`from\\s+['"]${pkg.replace(/[/@-]/g, '\\$&')}(['"/])`);
        if (pattern.test(code)) violations.push(`${relative(SRC, file)}: ${pkg}`);
      }
    }
    expect(violations).toEqual([]);
  });

  it('exactly one module imports the Polymarket SDK, and only createPublicClient', () => {
    const importers: { file: string; specifiers: string }[] = [];
    for (const file of sourceFiles) {
      const code = stripComments(readFileSync(file, 'utf8'));
      const match = code.match(/import\s+({[^}]*})\s+from\s+['"]@polymarket\/client['"]/);
      if (match) importers.push({ file: relative(SRC, file), specifiers: match[1] ?? '' });
    }

    expect(importers.map((i) => i.file)).toEqual(['polymarket/client.ts']);
    const imported = (importers[0]?.specifiers ?? '')
      .replace(/[{}]/g, '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    expect(imported).toEqual(['createPublicClient']);
  });

  it('the declared dependency set contains no signing or wallet package', () => {
    const pkg = JSON.parse(
      readFileSync(join(import.meta.dirname, '..', 'package.json'), 'utf8'),
    ) as { dependencies: Record<string, string> };
    const banned = ['ethers', 'viem', '@privy-io/server-auth', 'web3', '@polymarket/order-utils'];
    for (const name of banned) {
      expect(Object.keys(pkg.dependencies)).not.toContain(name);
    }
  });
});
