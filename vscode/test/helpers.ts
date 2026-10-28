import type { UiHost } from '../src/ext/core.ts';

export function makeHost(): { ui: UiHost; calls: Array<[string, ...unknown[]]> } {
  const calls: Array<[string, ...unknown[]]> = [];
  const rec = (n: string) => (...a: unknown[]) => void calls.push([n, ...a]);
  const ui: UiHost = {
    pickSession: async (list) => {
      calls.push(['pickSession', list.length]);
      return list[0]?.id;
    },
    pickAgentModel: async (a, m) => {
      calls.push(['pickAgentModel', a, m]);
      return { agent: 'writer' };
    },
    openSettings: rec('openSettings'),
    openFile: rec('openFile'),
    openExternal: rec('openExternal'),
    copy: rec('copy'),
    error: rec('error'),
    marks: rec('marks'),
    followStart: rec('followStart'),
    followEnd: rec('followEnd'),
    setFollow: rec('setFollow'),
  };
  return { ui, calls };
}
