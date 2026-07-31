import { readFileSync } from 'node:fs';

import { ConfigError, UsageError } from '../../core/errors.js';
import { submitSignal } from '../../ingress/client.js';
import { resolveDispatcher } from '../../ingress/dispatcher.js';
import { startIngressServer } from '../../ingress/server.js';
import { openStore } from '../../store/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

export async function ingressCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');

  switch (sub) {
    case 'serve': {
      const host = flags.str('host', process.env.PM_DESK_INGRESS_HOST ?? '127.0.0.1')!;
      const port = flags.int('port', Number(process.env.PM_DESK_INGRESS_PORT ?? 8787))!;
      const dispatch = flags.str('dispatch');
      flags.rejectUnknown('ingress serve');

      const secret = process.env.PM_DESK_INGRESS_SECRET;
      if (!secret) {
        throw new ConfigError('PM_DESK_INGRESS_SECRET is not set', {
          hint: "Generate one with:\n    node -e \"console.log(require('crypto').randomBytes(32).toString('hex'))\"\n  then export PM_DESK_INGRESS_SECRET=<value>. Never commit it.",
        });
      }
      if (
        host !== '127.0.0.1' &&
        host !== 'localhost' &&
        !flags.bool('i-understand-non-local-bind')
      ) {
        throw new UsageError(`refusing to bind the ingress to ${host}`, {
          hint: 'This ingress is designed to be local-only. If you really need a non-loopback bind, pass --i-understand-non-local-bind and put an authenticated reverse proxy in front of it.',
        });
      }

      const store = openStore({ home });
      const dispatcher = resolveDispatcher(store, dispatch);
      const server = await startIngressServer({
        store,
        secret,
        dispatcher,
        host,
        port,
        onEvent: ({ path, status, detail }) => {
          process.stdout.write(
            `${new Date().toISOString()} ${status} ${path}${detail ? ` ${detail}` : ''}\n`,
          );
        },
      });

      process.stdout.write(
        [
          `pm-desk ingress listening on ${server.url}  (PAPER ONLY)`,
          `  dispatcher   ${dispatcher.name}${dispatcher.name === 'outbox' ? ' (default — queues only, invokes nothing)' : ' (OPT-IN launcher enabled)'}`,
          `  desk home    ${store.home}`,
          `  auth         HMAC-SHA256 over "<timestamp>.<body>"`,
          '',
          'POST envelopes to /signals, or use: pm-desk ingress submit --file <signal.json>',
          'Press Ctrl-C to stop.',
          '',
        ].join('\n'),
      );

      await new Promise<void>((resolve) => {
        const shutdown = () => {
          void server.close().then(() => {
            store.close();
            resolve();
          });
        };
        process.once('SIGINT', shutdown);
        process.once('SIGTERM', shutdown);
      });
      return 0;
    }

    case 'submit': {
      const file = flags.required('file', 'Path to a JSON file holding one SignalEnvelope.');
      const url = flags.str('url', `http://127.0.0.1:${process.env.PM_DESK_INGRESS_PORT ?? 8787}`)!;
      flags.rejectUnknown('ingress submit');

      const secret = process.env.PM_DESK_INGRESS_SECRET;
      if (!secret) {
        throw new ConfigError('PM_DESK_INGRESS_SECRET is not set', {
          hint: 'Export the same secret the running ingress was started with.',
        });
      }

      let payload: unknown;
      try {
        payload = JSON.parse(readFileSync(file, 'utf8'));
      } catch (cause) {
        throw new UsageError(`cannot read a JSON signal envelope from ${file}`, {
          hint: 'Produce one with: pm-desk monitor evaluate --spec <spec> --json > signal.json',
          cause,
        });
      }

      const result = await submitSignal(payload, { url, secret });
      emit({ json }, { http_status: result.status, ...result.body }, () =>
        [`HTTP ${result.status}`, JSON.stringify(result.body, null, 2)].join('\n'),
      );
      return result.status >= 400 ? 1 : 0;
    }

    case 'outbox': {
      const status = flags.str('status');
      const limit = flags.int('limit', 25)!;
      flags.rejectUnknown('ingress outbox');
      const store = openStore({ home, readonly: true });
      const rows = store.signals
        .listOutbox(status as 'queued' | 'dispatched' | 'failed' | undefined, limit)
        .map((row) => ({
          id: row.id,
          signal_id: row.signal_id,
          status: row.status,
          dispatcher: row.dispatcher,
          queued_at: row.queued_at,
          artifact_ref: (row.artifact_ref ?? '').slice(0, 20),
        }));
      store.close();
      emit({ json }, { count: rows.length, outbox: rows }, () =>
        table(rows, ['id', 'signal_id', 'status', 'dispatcher', 'queued_at']),
      );
      return 0;
    }

    default:
      throw new UsageError(`unknown \`ingress\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk ingress serve | ingress submit --file <signal.json> | ingress outbox',
      });
  }
}
