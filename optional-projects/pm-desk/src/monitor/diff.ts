/**
 * Compact, bounded evidence excerpt for a content change.
 *
 * Not a general diff algorithm: the adjudicator needs to see *what changed*
 * without the envelope carrying a whole document. Common prefix and suffix are
 * trimmed on word boundaries and only the differing middle survives, with a
 * little context on each side.
 */
const DEFAULT_MAX_CHARS = 1200;
const CONTEXT_WORDS = 12;

export function diffExcerpt(
  previous: string | null | undefined,
  current: string,
  maxChars: number = DEFAULT_MAX_CHARS,
): string {
  if (previous === null || previous === undefined || previous === '') {
    return `+ ${truncate(current, maxChars)}`;
  }
  if (previous === current) return '(no textual change)';

  const before = previous.split(/\s+/);
  const after = current.split(/\s+/);

  let head = 0;
  while (head < before.length && head < after.length && before[head] === after[head]) head += 1;

  let tail = 0;
  while (
    tail < before.length - head &&
    tail < after.length - head &&
    before[before.length - 1 - tail] === after[after.length - 1 - tail]
  ) {
    tail += 1;
  }

  const contextStart = Math.max(0, head - CONTEXT_WORDS);
  const removed = before.slice(contextStart, before.length - tail + CONTEXT_WORDS).join(' ');
  const added = after.slice(contextStart, after.length - tail + CONTEXT_WORDS).join(' ');

  const half = Math.floor(maxChars / 2);
  return [`- ${truncate(removed, half)}`, `+ ${truncate(added, half)}`].join('\n');
}

function truncate(value: string, maxChars: number): string {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, Math.max(0, maxChars - 1))}…`;
}
