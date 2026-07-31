import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { AddressInfo } from 'node:net';

import { ConfigError } from '../core/errors.js';
import { nowIso } from '../core/time.js';
import { parseSignalEnvelope } from '../schema/signal.js';
import type { DeskStore } from '../store/index.js';
import type { Dispatcher } from './dispatcher.js';
import { SIGNATURE_HEADER, TIMESTAMP_HEADER, verifySignature } from './hmac.js';

/** Bodies above this are rejected before being read into memory or parsed. */
const MAX_BODY_BYTES = 1_000_000;

export interface IngressOptions {
  store: DeskStore;
  secret: string;
  dispatcher: Dispatcher;
  host?: string;
  port?: number;
  /** Called for each request outcome. Never receives secret material. */
  onEvent?: (event: { path: string; status: number; detail?: string }) => void;
}

export interface IngressServer {
  readonly host: string;
  readonly port: number;
  readonly url: string;
  close(): Promise<void>;
}

/**
 * Local-only signal ingress.
 *
 * Binds to loopback by default and authenticates every submission with HMAC.
 * The critical ordering is record-then-dispatch: a valid envelope is persisted
 * before anything downstream is invoked, so a dead adjudicator loses evidence
 * rather than the desk losing the signal. Idempotency is keyed on both
 * signal_id and dedupe_key, so replays and re-derived twins both collapse.
 */
export async function startIngressServer(options: IngressOptions): Promise<IngressServer> {
  if (!options.secret || options.secret.length < 32) {
    throw new ConfigError('the ingress needs a secret of at least 32 characters', {
      hint: "Generate one with `node -e \"console.log(require('crypto').randomBytes(32).toString('hex'))\"` and export it as PM_DESK_INGRESS_SECRET.",
    });
  }

  const host = options.host ?? '127.0.0.1';
  const server = createServer((req, res) => {
    void handle(req, res, options).catch((err: unknown) => {
      respond(res, 500, {
        error: err instanceof Error ? err.message : 'internal error',
      });
    });
  });

  const port = await new Promise<number>((resolve, reject) => {
    server.once('error', reject);
    server.listen(options.port ?? 8787, host, () => {
      resolve((server.address() as AddressInfo).port);
    });
  });

  return {
    host,
    port,
    url: `http://${host}:${port}`,
    close: () => closeServer(server),
  };
}

function closeServer(server: Server): Promise<void> {
  return new Promise((resolve) => {
    server.closeAllConnections?.();
    server.close(() => resolve());
  });
}

async function handle(
  req: IncomingMessage,
  res: ServerResponse,
  options: IngressOptions,
): Promise<void> {
  const path = (req.url ?? '/').split('?')[0];
  const emit = (status: number, detail?: string) =>
    options.onEvent?.({ path: path ?? '/', status, detail });

  if (path === '/health') {
    if (req.method !== 'GET') {
      emit(405);
      return respond(res, 405, { error: 'method not allowed' });
    }
    emit(200);
    return respond(res, 200, {
      status: 'ok',
      paper_only: true,
      schema_version: options.store.schemaVersion(),
      signals: options.store.signals.count(),
      queued: options.store.signals.listOutbox('queued').length,
      time: nowIso(),
    });
  }

  if (path !== '/signals') {
    emit(404);
    return respond(res, 404, { error: 'not found', hint: 'POST signal envelopes to /signals' });
  }

  if (req.method !== 'POST') {
    emit(405);
    return respond(res, 405, { error: 'method not allowed', hint: 'Use POST /signals' });
  }

  let raw: string;
  try {
    raw = await readBody(req);
  } catch (err) {
    const tooLarge = err instanceof Error && err.message === 'body_too_large';
    emit(tooLarge ? 413 : 400);
    return respond(res, tooLarge ? 413 : 400, {
      error: tooLarge ? `body exceeds ${MAX_BODY_BYTES} bytes` : 'could not read request body',
    });
  }

  // Authenticate before parsing: an unauthenticated caller gets no parser time.
  const verification = verifySignature(
    options.secret,
    header(req, TIMESTAMP_HEADER),
    raw,
    header(req, SIGNATURE_HEADER),
  );
  if (!verification.ok) {
    emit(401, verification.reason);
    return respond(res, 401, { error: 'unauthorized', reason: verification.reason });
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    emit(400);
    return respond(res, 400, { error: 'request body is not valid JSON' });
  }

  let envelope;
  try {
    envelope = parseSignalEnvelope(parsed);
  } catch (err) {
    emit(400);
    return respond(res, 400, {
      error: err instanceof Error ? err.message : 'invalid signal envelope',
    });
  }

  // Record first. Everything after this point can fail without losing evidence.
  const record = options.store.signals.record(envelope, 'ingress');
  const canonicalId = record.existing_signal_id ?? envelope.signal_id;

  // Idempotency is on *dispatch*, not on record: the monitor persists a signal
  // at emission time, and that must not consume its one trip downstream. So a
  // known signal that has never been dispatched is still dispatched here.
  if (!record.inserted && options.store.signals.hasOutboxFor(canonicalId)) {
    emit(200, 'duplicate');
    return respond(res, 200, {
      status: 'duplicate',
      signal_id: envelope.signal_id,
      existing_signal_id: record.existing_signal_id,
      dispatched: false,
      paper_only: true,
    });
  }

  // Dispatch the stored envelope, not the submitted one: a re-derived twin
  // carries a different signal_id, and the outbox references the recorded row.
  const toDispatch = record.inserted
    ? envelope
    : (options.store.signals.get(canonicalId)?.envelope ?? envelope);

  try {
    const result = await options.dispatcher.dispatch(toDispatch);
    emit(202, result.dispatcher);
    return respond(res, 202, {
      status: 'accepted',
      signal_id: envelope.signal_id,
      dispatched: true,
      dispatcher: result.dispatcher,
      artifact_ref: result.artifact_ref,
      paper_only: true,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    emit(202, `dispatch_failed: ${message}`);
    return respond(res, 202, {
      status: 'accepted',
      signal_id: envelope.signal_id,
      dispatched: false,
      dispatch_error: message,
      hint: 'The signal is recorded. Inspect it with `pm-desk ingress outbox`.',
      paper_only: true,
    });
  }
}

function header(req: IncomingMessage, name: string): string | undefined {
  const value = req.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let chunks: Buffer[] = [];
    let size = 0;
    let overflow = false;

    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        // Stop buffering so memory stays bounded, but keep draining the socket:
        // destroying it here would reset the connection and the client would see
        // a transport failure instead of the 413 we want to tell them about.
        overflow = true;
        chunks = [];
        return;
      }
      if (!overflow) chunks.push(chunk);
    });
    req.on('end', () => {
      if (overflow) reject(new Error('body_too_large'));
      else resolve(Buffer.concat(chunks).toString('utf8'));
    });
    req.on('error', reject);
  });
}

function respond(res: ServerResponse, status: number, body: unknown): void {
  if (res.headersSent) return;
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'content-type': 'application/json',
    'content-length': Buffer.byteLength(payload),
  });
  res.end(payload);
}
