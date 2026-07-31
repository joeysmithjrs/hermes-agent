import { readFileSync } from 'node:fs';

import { LedgerInvariantError, UsageError } from '../../core/errors.js';
import { recordFromAdjudication, recordManualEntry } from '../../ledger/ledger.js';
import { renderTelegramAlert } from '../../ledger/render.js';
import { parseAdjudication } from '../../schema/adjudication.js';
import { openStore } from '../../store/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

const MANUAL_CONFIRM_FLAG = 'i-am-recording-a-paper-entry-manually';

export async function ledgerCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');

  switch (sub) {
    case 'record': {
      const adjudicationPath = flags.str('adjudication');
      const manual = flags.bool('manual');
      // Each branch reads its own flags, so rejectUnknown runs per-branch below.

      const store = openStore({ home });
      try {
        if (adjudicationPath) {
          const payload = readJson(adjudicationPath, 'adjudication');
          const adjudication = parseAdjudication(payload);
          flags.rejectUnknown('ledger record');
          const entry = recordFromAdjudication(store, adjudication);
          const stored = store.signals.get(adjudication.signal_id)!;
          emit({ json }, { entry, paper_only: true }, () =>
            [
              `recorded PAPER ONLY ledger entry ${entry.entry_id}`,
              '',
              renderTelegramAlert({ entry, signal: stored.envelope, adjudication }),
            ].join('\n'),
          );
          return 0;
        }

        if (manual) {
          const acknowledged = flags.bool(MANUAL_CONFIRM_FLAG);
          const input = {
            thesis: flags.required('thesis', 'A specific, falsifiable paper thesis.'),
            market_id: flags.str('market'),
            condition_id: flags.str('condition'),
            token_id: flags.str('token'),
            outcome: flags.str('outcome', 'YES') as 'YES' | 'NO',
            assumed_size_usd: Number(flags.str('size', '100')),
            slippage_rule: (flags.str('slippage', 'mid_no_slippage') ?? 'mid_no_slippage') as
              'cross_spread_full' | 'mid_plus_1_tick' | 'mid_no_slippage',
            expiry_horizon_s: flags.int('expiry-s', 86_400)!,
            markout_horizons_s: flags.list('markout-s').map(Number).filter(Number.isFinite),
            invalidations: flags.list('invalidation'),
            entry_mid: numberOrNull(flags.str('mid')),
            entry_best_bid: numberOrNull(flags.str('bid')),
            entry_best_ask: numberOrNull(flags.str('ask')),
            acknowledged,
          };
          flags.rejectUnknown('ledger record --manual');

          if (input.markout_horizons_s.length === 0) input.markout_horizons_s = [300, 3600];
          if (input.invalidations.length === 0) {
            throw new LedgerInvariantError('a manual entry needs at least one --invalidation', {
              hint: 'State what would prove the thesis wrong, e.g. --invalidation "a later revision above 3.0%".',
            });
          }

          const entry = recordManualEntry(store, input);
          emit(
            { json },
            { entry, paper_only: true },
            () =>
              `recorded PAPER ONLY manual ledger entry ${entry.entry_id} (assumed ${entry.outcome} @ ${entry.assumed_entry_price})`,
          );
          return 0;
        }

        throw new UsageError('ledger record needs --adjudication <file> or --manual', {
          hint: `A ledger entry comes from a validated paper_alert adjudication, or from an explicit operator command with --manual --${MANUAL_CONFIRM_FLAG}.`,
        });
      } finally {
        store.close();
      }
    }

    case 'list': {
      const limit = flags.int('limit', 25)!;
      flags.rejectUnknown('ledger list');
      const store = openStore({ home, readonly: true });
      const rows = store.ledger.list(limit).map((e) => ({
        entry_id: e.entry_id,
        decided_at: e.decided_at,
        outcome: e.outcome ?? '',
        entry_px: e.assumed_entry_price ?? '',
        size_usd: e.assumed_size_usd,
        origin: e.origin,
        fill_type: e.fill_type,
      }));
      store.close();
      emit({ json }, { count: rows.length, paper_only: true, entries: rows }, () =>
        [
          'PAPER ONLY — no row here represents a real fill.',
          '',
          table(rows, ['entry_id', 'decided_at', 'outcome', 'entry_px', 'size_usd', 'fill_type']),
        ].join('\n'),
      );
      return 0;
    }

    case 'show': {
      const entryId = flags.required('entry', 'An entry_id from `pm-desk ledger list`.');
      flags.rejectUnknown('ledger show');
      const store = openStore({ home, readonly: true });
      const entry = store.ledger.get(entryId);
      if (!entry) {
        store.close();
        throw new UsageError(`no ledger entry ${entryId}`, {
          hint: 'List entries with `pm-desk ledger list`.',
        });
      }
      const annotations = store.ledger.listAnnotations(entryId);
      store.close();
      emit({ json }, { entry, annotations, paper_only: true }, () =>
        [JSON.stringify(entry, null, 2), '', `annotations: ${annotations.length}`].join('\n'),
      );
      return 0;
    }

    case 'annotate': {
      const entryId = flags.required('entry', 'An entry_id from `pm-desk ledger list`.');
      const kind = flags.str('kind', 'note')!;
      const note = flags.str('note');
      const detail = flags.str('detail');
      flags.rejectUnknown('ledger annotate');

      if (!['markout', 'outcome', 'note', 'invalidated'].includes(kind)) {
        throw new UsageError(`--kind must be markout, outcome, note or invalidated (got ${kind})`, {
          hint: 'Use markout for a later price check, outcome for resolution.',
        });
      }

      const store = openStore({ home });
      try {
        const id = store.ledger.annotate({
          entry_id: entryId,
          recorded_at: new Date().toISOString(),
          kind: kind as 'markout' | 'outcome' | 'note' | 'invalidated',
          note,
          detail: detail ? JSON.parse(detail) : undefined,
        });
        emit(
          { json },
          { annotation_id: id, entry_id: entryId, kind },
          () => `annotated ${entryId} with a ${kind} (id ${id})`,
        );
      } finally {
        store.close();
      }
      return 0;
    }

    case 'render': {
      const entryId = flags.str('entry');
      const signalId = flags.str('signal');
      flags.rejectUnknown('ledger render');

      const store = openStore({ home, readonly: true });
      try {
        const entry = entryId ? store.ledger.get(entryId) : undefined;
        if (entryId && !entry) {
          throw new UsageError(`no ledger entry ${entryId}`, {
            hint: 'Try `pm-desk ledger list`.',
          });
        }
        const resolvedSignalId = entry?.signal_id ?? signalId;
        if (!resolvedSignalId) {
          throw new UsageError('ledger render needs --entry <id> or --signal <id>', {
            hint: 'A manual entry with no linked signal cannot be rendered as a signal alert.',
          });
        }
        const stored = store.signals.get(resolvedSignalId);
        if (!stored) {
          throw new UsageError(`no signal ${resolvedSignalId} in this store`, {
            hint: 'Submit it through the ingress first.',
          });
        }
        const recorded = store.adjudications.get(resolvedSignalId);
        if (!recorded) {
          throw new UsageError(`no adjudication recorded for ${resolvedSignalId}`, {
            hint: 'Record one with `pm-desk workflow adjudicate --signal <id> --result <file>`.',
          });
        }
        const adjudication = parseAdjudication(recorded.adjudication);
        const text = renderTelegramAlert({ signal: stored.envelope, adjudication, entry });
        emit({ json }, { text, paper_only: true }, () => text);
      } finally {
        store.close();
      }
      return 0;
    }

    case 'export': {
      const limit = flags.int('limit', 1000)!;
      flags.rejectUnknown('ledger export');
      const store = openStore({ home, readonly: true });
      const entries = store.ledger.list(limit).map((entry) => ({
        ...entry,
        annotations: store.ledger.listAnnotations(entry.entry_id),
      }));
      store.close();
      emit({ json: true }, { paper_only: true, count: entries.length, entries }, () => '');
      return 0;
    }

    default:
      throw new UsageError(`unknown \`ledger\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk ledger record --adjudication <file> | ledger list | ledger show --entry <id> | ledger annotate | ledger render | ledger export',
      });
  }
}

function readJson(path: string, label: string): unknown {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (cause) {
    throw new UsageError(`cannot read a JSON ${label} from ${path}`, {
      hint: `Check the path and that the file contains a single JSON ${label} object.`,
      cause,
    });
  }
}

function numberOrNull(value: string | undefined): number | null {
  if (value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}
