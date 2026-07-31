import { readFileSync } from 'node:fs';

import { UsageError } from '../../core/errors.js';
import { recordFromAdjudication } from '../../ledger/ledger.js';
import { renderTelegramAlert } from '../../ledger/render.js';
import { parseAdjudication } from '../../schema/adjudication.js';
import { parseSignalEnvelope } from '../../schema/signal.js';
import { openStore } from '../../store/index.js';
import { renderAdjudicationPrompt } from '../../workflow/prompt.js';
import type { Flags } from '../args.js';
import { emit } from '../output.js';

export async function workflowCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');

  switch (sub) {
    case 'render': {
      const signalId = flags.str('signal');
      const file = flags.str('file');
      flags.rejectUnknown('workflow render');

      const store = openStore({ home, readonly: true });
      try {
        let signal;
        if (file) {
          signal = parseSignalEnvelope(JSON.parse(readFileSync(file, 'utf8')));
        } else if (signalId) {
          const stored = store.signals.get(signalId);
          if (!stored) {
            throw new UsageError(`no signal ${signalId} in this store`, {
              hint: 'List recorded signals with `pm-desk store export --table signals`.',
            });
          }
          signal = stored.envelope;
        } else {
          throw new UsageError('workflow render needs --signal <id> or --file <signal.json>', {
            hint: 'This renders the adjudication prompt locally. It calls no model.',
          });
        }

        const prompt = renderAdjudicationPrompt(store, signal);
        emit({ json }, { signal_id: signal.signal_id, prompt, paper_only: true }, () => prompt);
      } finally {
        store.close();
      }
      return 0;
    }

    case 'adjudicate': {
      // Records an adjudication produced elsewhere (by the workflow, or by hand
      // for testing). This command never calls a model itself.
      const resultPath = flags.required('result', 'Path to a JSON adjudication result.');
      flags.rejectUnknown('workflow adjudicate');

      const store = openStore({ home });
      try {
        const adjudication = parseAdjudication(JSON.parse(readFileSync(resultPath, 'utf8')));
        const stored = store.signals.get(adjudication.signal_id);
        if (!stored) {
          throw new UsageError(`no signal ${adjudication.signal_id} in this store`, {
            hint: 'Submit the signal through the ingress before recording its adjudication.',
          });
        }

        if (adjudication.decision === 'paper_alert') {
          const entry = recordFromAdjudication(store, adjudication);
          const text = renderTelegramAlert({ signal: stored.envelope, adjudication, entry });
          emit(
            { json },
            { decision: adjudication.decision, entry, telegram_message: text, paper_only: true },
            () => text,
          );
          return 0;
        }

        store.adjudications.record(adjudication, new Date().toISOString());
        const text = renderTelegramAlert({ signal: stored.envelope, adjudication });
        emit(
          { json },
          {
            decision: adjudication.decision,
            entry: null,
            telegram_message: text,
            paper_only: true,
          },
          () =>
            adjudication.decision === 'ignore'
              ? `recorded: ignore (no alert delivered)\n\n${adjudication.rationale}`
              : text,
        );
        return 0;
      } finally {
        store.close();
      }
    }

    default:
      throw new UsageError(`unknown \`workflow\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk workflow render --signal <id> | workflow adjudicate --result <file>',
      });
  }
}
