import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { DispatchDisabledError } from '../src/core/errors.js';
import { fixedClock } from '../src/core/time.js';
import { HermesLauncherDispatcher, OutboxDispatcher } from '../src/ingress/dispatcher.js';
import { signPayload, verifySignature } from '../src/ingress/hmac.js';
import { startIngressServer, type IngressServer } from '../src/ingress/server.js';
import { openStore, type DeskStore } from '../src/store/index.js';

const SECRET = 'a'.repeat(64);

const ENVELOPE = {
  version: 1,
  signal_id: 'sig_' + '1'.repeat(32),
  kind: 'primary_source_change',
  severity: 'high',
  observed_at: '2026-07-30T12:00:00.000Z',
  rule_id: 'gdp_release_change',
  rule_version: '1',
  market_refs: [],
  source_refs: [
    {
      source_id: 'example_official_release',
      url: 'https://example.gov/release',
      previous_hash: 'a'.repeat(64),
      current_hash: 'b'.repeat(64),
      artifact_ref: 'sha256:' + 'b'.repeat(64),
    },
  ],
  evidence: { claims: ['Q1 GDP revised 3.1% -> 2.4%'] },
  paper_only: true,
  dedupe_key: 'gdp_release_change:1:' + 'b'.repeat(64),
};

let dir: string;
let store: DeskStore;
let server: IngressServer;

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-ing-'));
  store = openStore({ home: dir });
});

afterEach(async () => {
  if (server) await server.close();
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

async function post(
  body: unknown,
  options: { secret?: string; signature?: string; timestamp?: string } = {},
) {
  const raw = typeof body === 'string' ? body : JSON.stringify(body);
  const timestamp = options.timestamp ?? new Date().toISOString();
  const signature = options.signature ?? signPayload(options.secret ?? SECRET, timestamp, raw);

  const response = await fetch(`${server.url}/signals`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      'x-pm-desk-timestamp': timestamp,
      'x-pm-desk-signature': signature,
    },
    body: raw,
  });
  const text = await response.text();
  return {
    status: response.status,
    body: text ? (JSON.parse(text) as Record<string, unknown>) : undefined,
  };
}

describe('HMAC authentication', () => {
  const AT = '2026-07-30T12:00:00.000Z';
  // Pin the clock so these assert signature behaviour, not replay-window timing.
  const at = { clock: fixedClock(AT) };

  it('round-trips a signature over timestamp and body', () => {
    const sig = signPayload(SECRET, AT, '{"a":1}');
    expect(sig).toMatch(/^sha256=[0-9a-f]{64}$/);
    expect(verifySignature(SECRET, AT, '{"a":1}', sig, at).ok).toBe(true);
  });

  it('rejects a body tampered with after signing', () => {
    const sig = signPayload(SECRET, AT, '{"a":1}');
    expect(verifySignature(SECRET, AT, '{"a":2}', sig, at).ok).toBe(false);
  });

  it('rejects a signature made with a different secret', () => {
    const sig = signPayload('b'.repeat(64), AT, '{"a":1}');
    expect(verifySignature(SECRET, AT, '{"a":1}', sig, at).ok).toBe(false);
  });

  it('rejects a stale timestamp so a captured request cannot be replayed later', () => {
    const old = '2026-07-30T12:00:00.000Z';
    const sig = signPayload(SECRET, old, '{"a":1}');
    const result = verifySignature(SECRET, old, '{"a":1}', sig, {
      clock: fixedClock('2026-07-30T13:00:00.000Z'),
      toleranceSeconds: 300,
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toMatch(/timestamp/i);
  });

  it('never echoes the secret in a failure reason', () => {
    const result = verifySignature(SECRET, AT, '{}', 'sha256=deadbeef', at);
    expect(result.ok).toBe(false);
    expect(JSON.stringify(result)).not.toContain(SECRET);
  });
});

describe('ingress server', () => {
  beforeEach(async () => {
    server = await startIngressServer({
      store,
      secret: SECRET,
      dispatcher: new OutboxDispatcher(store),
      host: '127.0.0.1',
      port: 0,
    });
  });

  it('binds to loopback only', () => {
    expect(server.host).toBe('127.0.0.1');
    expect(server.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
  });

  it('accepts a valid signed envelope, records it and queues it', async () => {
    const response = await post(ENVELOPE);
    expect(response.status).toBe(202);
    expect(response.body).toMatchObject({
      status: 'accepted',
      signal_id: ENVELOPE.signal_id,
      dispatched: true,
      paper_only: true,
    });

    expect(store.signals.count()).toBe(1);
    const outbox = store.signals.listOutbox('queued');
    expect(outbox).toHaveLength(1);
    expect(outbox[0]?.signal_id).toBe(ENVELOPE.signal_id);
  });

  it('is idempotent: a replayed submission records and queues nothing new', async () => {
    await post(ENVELOPE);
    const second = await post(ENVELOPE);

    expect(second.status).toBe(200);
    expect(second.body).toMatchObject({ status: 'duplicate', dispatched: false });
    expect(store.signals.count()).toBe(1);
    expect(store.signals.listOutbox()).toHaveLength(1);
  });

  it('treats a fresh signal_id carrying a known dedupe_key as a duplicate', async () => {
    await post(ENVELOPE);
    const twin = { ...ENVELOPE, signal_id: 'sig_' + '2'.repeat(32) };
    const response = await post(twin);

    expect(response.body).toMatchObject({ status: 'duplicate' });
    expect(store.signals.count()).toBe(1);
  });

  it('still dispatches a signal the monitor already recorded but never dispatched', async () => {
    // The monitor persists at emission time; that must not consume the one
    // trip downstream, or a monitor-emitted signal would never reach anyone.
    store.signals.record(ENVELOPE as never, 'monitor');
    expect(store.signals.listOutbox()).toHaveLength(0);

    const response = await post(ENVELOPE);
    expect(response.status).toBe(202);
    expect(response.body).toMatchObject({ status: 'accepted', dispatched: true });
    expect(store.signals.count()).toBe(1);
    expect(store.signals.listOutbox('queued')).toHaveLength(1);

    // And a second submission is now a duplicate.
    expect((await post(ENVELOPE)).body).toMatchObject({ status: 'duplicate' });
    expect(store.signals.listOutbox()).toHaveLength(1);
  });

  it('queues under the recorded signal_id when a twin is submitted first', async () => {
    store.signals.record(ENVELOPE as never, 'monitor');
    const twin = { ...ENVELOPE, signal_id: 'sig_' + '3'.repeat(32) };

    const response = await post(twin);
    expect(response.status).toBe(202);
    // The outbox row must point at a signal that actually exists.
    expect(store.signals.listOutbox()[0]?.signal_id).toBe(ENVELOPE.signal_id);
  });

  it('rejects an unsigned request', async () => {
    const response = await fetch(`${server.url}/signals`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(ENVELOPE),
    });
    expect(response.status).toBe(401);
    expect(store.signals.count()).toBe(0);
  });

  it('rejects a request signed with the wrong secret', async () => {
    const response = await post(ENVELOPE, { secret: 'f'.repeat(64) });
    expect(response.status).toBe(401);
    expect(store.signals.count()).toBe(0);
  });

  it('rejects a schema-invalid envelope with 400 and stores nothing', async () => {
    const response = await post({ ...ENVELOPE, paper_only: false });
    expect(response.status).toBe(400);
    expect(String(response.body?.error)).toMatch(/paper_only/);
    expect(store.signals.count()).toBe(0);
  });

  it('rejects a payload that is not JSON', async () => {
    const response = await post('not json at all');
    expect(response.status).toBe(400);
  });

  it('rejects an oversized body before parsing it', async () => {
    const huge = { ...ENVELOPE, evidence: { claims: ['x'.repeat(2_000_000)] } };
    const response = await post(huge);
    expect(response.status).toBe(413);
    expect(store.signals.count()).toBe(0);
  });

  it('rejects a wrong method and an unknown path', async () => {
    const get = await fetch(`${server.url}/signals`);
    expect(get.status).toBe(405);
    const unknown = await fetch(`${server.url}/nope`, { method: 'POST' });
    expect(unknown.status).toBe(404);
  });

  it('serves a health endpoint that needs no signature and leaks nothing', async () => {
    const response = await fetch(`${server.url}/health`);
    expect(response.status).toBe(200);
    const body = (await response.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ status: 'ok', paper_only: true });
    expect(JSON.stringify(body)).not.toContain(SECRET);
  });
});

describe('dispatchers', () => {
  it('the default dispatcher writes a durable outbox artifact and calls no LLM', async () => {
    const dispatcher = new OutboxDispatcher(store);
    const envelope = { ...ENVELOPE } as never;
    store.signals.record(envelope, 'test');

    const result = await dispatcher.dispatch(envelope);
    expect(result.dispatcher).toBe('outbox');
    expect(result.artifact_ref).toMatch(/^sha256:[0-9a-f]{64}$/);

    // The queued payload is the exact envelope, retrievable for later handoff.
    const queued = JSON.parse(store.artifacts.read(result.artifact_ref!)) as Record<
      string,
      unknown
    >;
    expect(queued.signal_id).toBe(ENVELOPE.signal_id);
    expect(store.signals.listOutbox('queued')).toHaveLength(1);
  });

  it('the Hermes launcher refuses to run unless explicitly enabled', async () => {
    const calls: string[][] = [];
    const dispatcher = new HermesLauncherDispatcher(store, {
      enabled: false,
      runner: async (cmd, args) => {
        calls.push([cmd, ...args]);
        return { code: 0, stdout: '', stderr: '' };
      },
    });

    await expect(dispatcher.dispatch(ENVELOPE as never)).rejects.toThrow(DispatchDisabledError);
    expect(calls).toHaveLength(0);
  });

  it('launches the workflow with the argument shape the real Hermes CLI accepts', async () => {
    const calls: { cmd: string; args: string[] }[] = [];
    const dispatcher = new HermesLauncherDispatcher(store, {
      enabled: true,
      binary: 'hermes',
      workflow: 'workflows/pm-signal-adjudication-v0.yaml',
      runner: async (cmd, args) => {
        calls.push({ cmd, args });
        return { code: 0, stdout: 'run_id: r1', stderr: '' };
      },
    });
    store.signals.record(ENVELOPE as never, 'test');

    const result = await dispatcher.dispatch(ENVELOPE as never);
    expect(result.dispatcher).toBe('hermes');
    expect(calls).toHaveLength(1);
    expect(calls[0]?.cmd).toBe('hermes');

    // `hermes workflow run <path> --input '<json>'` — there is no --input-file.
    expect(calls[0]?.args.slice(0, 3)).toEqual([
      'workflow',
      'run',
      'workflows/pm-signal-adjudication-v0.yaml',
    ]);
    const inputIndex = calls[0]!.args.indexOf('--input');
    expect(inputIndex).toBeGreaterThan(-1);

    // The input is one argv element, so nothing is shell-interpreted.
    const payload = JSON.parse(calls[0]!.args[inputIndex + 1]!) as Record<string, unknown>;
    expect(payload.signal_id).toBe(ENVELOPE.signal_id);
    expect(payload.paper_only).toBe(true);

    // The workflow's agent node has tools: [], so it cannot fetch its own
    // evidence — the launcher must hand it a fully rendered prompt.
    expect(String(payload.prompt)).toContain('PAPER ONLY');
    expect(String(payload.prompt)).toContain(ENVELOPE.signal_id);
    expect(String(payload.prompt)).toContain('https://example.gov/release');
  });

  it('uses run-catalog when the workflow is a registered catalog id', async () => {
    const calls: string[][] = [];
    const dispatcher = new HermesLauncherDispatcher(store, {
      enabled: true,
      workflow: 'pm-signal-adjudication-v0',
      mode: 'catalog',
      maxBudgetUsd: 0.35,
      runner: async (cmd, args) => {
        calls.push([cmd, ...args]);
        return { code: 0, stdout: '', stderr: '' };
      },
    });
    store.signals.record(ENVELOPE as never, 'test');
    await dispatcher.dispatch(ENVELOPE as never);

    expect(calls[0]?.slice(0, 4)).toEqual([
      'hermes',
      'workflow',
      'run-catalog',
      'pm-signal-adjudication-v0',
    ]);
    expect(calls[0]).toContain('--max-budget-usd');
    expect(calls[0]).toContain('0.35');
  });

  it('records a launcher failure as a failed outbox row rather than losing the signal', async () => {
    const dispatcher = new HermesLauncherDispatcher(store, {
      enabled: true,
      runner: async () => ({ code: 1, stdout: '', stderr: 'hermes: workflow not found' }),
    });
    store.signals.record(ENVELOPE as never, 'test');

    await expect(dispatcher.dispatch(ENVELOPE as never)).rejects.toThrow(/workflow not found/);
    const outbox = store.signals.listOutbox('failed');
    expect(outbox).toHaveLength(1);
    expect(outbox[0]?.signal_id).toBe(ENVELOPE.signal_id);
  });
});

describe('server + dispatcher integration', () => {
  it('records the signal even when the dispatcher throws', async () => {
    server = await startIngressServer({
      store,
      secret: SECRET,
      dispatcher: {
        name: 'exploding',
        dispatch: async () => {
          throw new Error('downstream is down');
        },
      },
      host: '127.0.0.1',
      port: 0,
    });

    const response = await post(ENVELOPE);
    // Record-first: the evidence survives a downstream outage.
    expect(store.signals.count()).toBe(1);
    expect(response.status).toBe(202);
    expect(response.body).toMatchObject({ status: 'accepted', dispatched: false });
    expect(String(response.body?.dispatch_error)).toMatch(/downstream is down/);
  });
});
