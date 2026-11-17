const BASE = '/api';

async function req<T>(path: string, opts?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...opts?.headers },
    ...opts,
  });
  if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
  return res.json();
}

export const api = {
  getConfig: () => req<Record<string, any>>('/config'),
  saveConfig: (data: Record<string, any>) =>
    req('/config', { method: 'POST', body: JSON.stringify(data) }),
  getConfigStatus: () =>
    req<{ configured: boolean; has_tmdb: boolean; has_auth: boolean }>('/config/status'),

  getStatus: () =>
    req<{ tunarr: ConnStatus; plex: ConnStatus }>('/status'),
  // Tests credentials the user has typed but not yet saved — onboarding needs to
  // validate before writing, or a typo'd URL produces a green "Setup complete".
  testConnection: (data: {
    tunarr_url?: string; tunarr_username?: string; tunarr_password?: string;
    plex_url?: string; plex_token?: string;
  }) =>
    req<{ tunarr?: ConnStatus; plex?: ConnStatus }>('/test-connection', {
      method: 'POST', body: JSON.stringify(data),
    }),
  updateCheck: (current: string) =>
    req<UpdateInfo>(`/update-check?current=${encodeURIComponent(current)}`),
  getTunarrChannels: () => req<TunarrChannel[]>('/tunarr/channels'),
  getGuide: () => req<Guide>('/guide'),
  getFillerLists: () => req<FillerList[]>('/tunarr/filler-lists'),

  getChannels: () => req<ChannelsFile>('/channels'),
  // The in-progress Planner draft (compose + AI extras), distinct from the deployed /channels.
  getDraft: () => req<ChannelsFile>('/pipeline/draft'),
  updateChannels: (data: object) =>
    req('/channels', { method: 'PUT', body: JSON.stringify(data) }),
  getChannel: (n: number) => req<Channel>(`/channels/${n}`),
  updateChannel: (n: number, ch: object) =>
    req(`/channels/${n}`, { method: 'PUT', body: JSON.stringify(ch) }),
  deleteChannel: (n: number) => req(`/channels/${n}`, { method: 'DELETE' }),
  setChannelIcon: (n: number, body: { mode: 'badge' | 'tmdb' | 'custom' | 'clear'; url?: string }) =>
    req<{ ok: boolean; mode: string; url: string }>(`/channels/${n}/icon`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getLibraryTitles: () => req<string[]>('/library/titles'),
  // Every movie/show with exactly this title, each with a ready-to-save entry.
  lookupLibrary: (title: string, kind?: 'movie' | 'show') =>
    req<{ matches: LibraryMatch[] }>(
      `/library/lookup?title=${encodeURIComponent(title)}${kind ? `&kind=${kind}` : ''}`),

  getCsvInfo: () => req<CsvInfo>('/pipeline/csv/info'),
  getFacets: (minItems = 5) => req<LibraryFacets>(`/pipeline/facets?min_items=${minItems}`),
  getProgrammingBlocks: () => req<ProgrammingBlock[]>('/pipeline/programming-blocks'),
  getFranchises: (refresh = false) =>
    req<FranchiseCandidate[]>(`/pipeline/franchises${refresh ? '?refresh=1' : ''}`),
  startTmdbScan: (refresh = false) =>
    req<{ running: boolean; cached: boolean; reason?: string }>(
      `/pipeline/tmdb-scan${refresh ? '?refresh=1' : ''}`,
      { method: 'POST' },
    ),
  tmdbScanStatus: () =>
    req<{ running: boolean; scanned: number; total: number; done: boolean }>(
      '/pipeline/tmdb-scan/status',
    ),
  startTvmazeScan: (refresh = false) =>
    req<{ running: boolean; cached: boolean; reason?: string }>(
      `/pipeline/tvmaze-scan${refresh ? '?refresh=1' : ''}`,
      { method: 'POST' },
    ),
  tvmazeScanStatus: () =>
    req<{ running: boolean; scanned: number; total: number; done: boolean }>(
      '/pipeline/tvmaze-scan/status',
    ),
  getPrompt: (target?: string, prefs?: string, start?: number) => {
    const p = new URLSearchParams();
    if (target) p.set('target', target);
    if (prefs) p.set('preferences', prefs);
    if (start !== undefined && start !== 10) p.set('start', String(start));
    return req<{ content: string }>(`/pipeline/prompt?${p}`);
  },
  buildPrompt: (opts: PromptOptions) =>
    req<{ content: string }>('/pipeline/prompt', {
      method: 'POST',
      body: JSON.stringify(opts),
    }),
  composeChannels: (
    specs: CandidateSpec[],
    start: number,
    opts?: { live?: boolean; commercials?: Commercials },
  ) =>
    req<ComposeResult>('/pipeline/compose', {
      method: 'POST',
      body: JSON.stringify({ specs, start, live: opts?.live, commercials: opts?.commercials }),
    }),
  validateText: async (content: string, append = false) => {
    const form = new FormData();
    form.append('content', content);
    if (append) form.append('append', 'true');
    const res = await fetch(`${BASE}/pipeline/validate`, { method: 'POST', body: form });
    return res.json() as Promise<ValidateResult>;
  },
  validateFile: async (file: File, append = false) => {
    const form = new FormData();
    form.append('file', file);
    if (append) form.append('append', 'true');
    const res = await fetch(`${BASE}/pipeline/validate`, { method: 'POST', body: form });
    return res.json() as Promise<ValidateResult>;
  },
  getDiscoverPrompt: (discover = true, curate_pools: string[] = []) =>
    req<{ content: string; start: number; existing_count: number }>('/pipeline/discover-prompt', {
      method: 'POST',
      body: JSON.stringify({ discover, curate_pools }),
    }),

  getLogs: () => req<LogEntry[]>('/logs'),
  getLog: (name: string) => req<{ name: string; content: string }>(`/logs/${name}`),

  getLibraries: () => req<PlexLibrary[]>('/pipeline/libraries'),
  getCollections: () => req<PlexCollection[]>('/pipeline/collections'),
  applyCollections: (selections: CollectionSelection[]) =>
    req<{ ok: boolean; added: number }>('/pipeline/collections/apply', {
      method: 'POST',
      body: JSON.stringify(selections),
    }),
  applyChannel: (n: number) =>
    req<{ ok: boolean; number: number; program_count: number }>(
      `/channels/${n}/apply`, { method: 'POST' }),

  // ── Planner state ──
  getPlannerState: () => req<PlannerStateFile>('/pipeline/planner-state'),
  savePlannerState: (state: PlannerStateFile) =>
    req<{ ok: boolean }>('/pipeline/planner-state', {
      method: 'PUT',
      body: JSON.stringify(state),
    }),
  deletePlannerState: () =>
    req<{ ok: boolean }>('/pipeline/planner-state', { method: 'DELETE' }),

  // ── Deploy preview (diff without Tunarr writes) ──
  deployPreview: (mode: 'edit' | 'nuke') =>
    req<DeployPreviewResult>('/pipeline/deploy-preview', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),

  // ── Surgical deploy (Add/Edit mode) ──
  surgicalDeploy: () =>
    fetch(`${BASE}/pipeline/surgical-deploy`, { method: 'POST' }),

  // ── Live channels (recipes) ──
  previewRecipe: (value: string, order?: string | null, exclude: string[] = [], match: 'title_contains' | 'franchise' = 'title_contains') =>
    req<RecipePreview>('/recipes/preview', {
      method: 'POST',
      body: JSON.stringify({ value, order, exclude, match }),
    }),
  getRecipesStatus: () => req<RecipesStatus>('/recipes/status'),
  runRecipes: (apply: boolean, only?: number) =>
    req<CycleSummary>(
      `/recipes/run?apply=${apply}${only !== undefined ? `&only=${only}` : ''}`,
      { method: 'POST' },
    ),
  pauseRecipes: (paused: boolean) =>
    req<RecipesStatus>(`/recipes/pause?paused=${paused}`, { method: 'POST' }),
  saveRecipeConfig: (enabled: boolean, interval_hours: number) =>
    req<RecipesStatus>('/recipes/config', {
      method: 'POST',
      body: JSON.stringify({ enabled, interval_hours }),
    }),
};

// ── Types ──────────────────────────────────────────────────────────────────────

export interface PlexServer { name: string; url: string; token: string }
export interface ConnStatus {
  ok: boolean; url: string; error?: string;
  version?: string | null;
  // Set when Plex let us in WITHOUT a token (LAN-permissive server), so a
  // green result does not actually vouch for the token.
  token_unverified?: boolean;
  note?: string;
}
export interface UpdateInfo {
  enabled: boolean;
  update_available?: boolean;
  current?: string;
  latest?: string | null;
  name?: string;
  url?: string;
}
export interface TunarrChannel { number: number; name: string; id?: string }
export interface GuideChannel { number: number; name: string; icon?: string | null }
export interface GuideProgramme { number: number; start: string; stop: string; title: string; episode?: string }
export interface Guide { channels: GuideChannel[]; programmes: GuideProgramme[]; error?: string }
export interface FillerList { id: string; name: string; contentCount: number }
export interface LibraryMatch { kind: 'movie' | 'show'; title: string; year: number | null; entry: MovieRef | ShowRef }
// Commercials: a channel can pull from a Tunarr filler list, played in gaps
// between shows (pad_minutes controls the gap size). Absent = commercials off.
export interface Commercials { filler_list_id: string; filler_list_name?: string; pad_minutes?: number }
export interface PlaybackSetting { structure: 'interleaved' | 'timeline'; episodes_per_block?: number }
export interface MatchRef { match: 'title_contains'; value: string; order?: string | null; exclude?: string[] }
export interface FranchiseRef { match: 'franchise'; name: string; order?: string | null; exclude?: string[] }
// { movie } / { show }: a title pinned to one media type (a movie and a show can share a title).
// Saved from the Add box they also carry the item's own numbers (`ids`) and `year`, which
// find that exact item even when two things share a title (the two Aladdins).
export interface ItemNumbers { year?: number; ids?: Record<string, string> }
export type MovieRef = { movie: string } & ItemNumbers;
export type ShowRef = { show: string } & ItemNumbers;
export type ContentItem = string | { collection: string } | MovieRef | ShowRef | MatchRef | FranchiseRef;
export function isMatchRef(c: ContentItem): c is MatchRef {
  return typeof c === 'object' && c !== null && 'match' in c;
}
export function isCollectionRef(c: ContentItem): c is { collection: string } {
  return typeof c === 'object' && c !== null && 'collection' in c;
}
export interface ChannelIcon { mode: 'badge' | 'tmdb' | 'custom'; url?: string; pinned?: boolean }
export interface Channel {
  number: number;
  name: string;
  shuffle: 'ordered' | 'shuffle' | 'block';
  content: ContentItem[];
  live?: boolean;
  commercials?: Commercials;
  icon?: ChannelIcon;
  playback?: PlaybackSetting;
}

// ── Live channels (recipes) ──
export interface RecipeMatch { title: string; year: number | null }
export interface RecipePreview { value: string; order: string | null; count: number; matches: RecipeMatch[] }
export interface CycleChange { number: number; name: string; added: string[]; added_count: number; removed_count: number; applied: boolean }
export interface CycleSkip { number: number; name: string; reason: string }
export interface CycleSummary {
  time: string; apply: boolean; live: number; changed: number;
  changes: CycleChange[]; skipped: CycleSkip[]; error: string | null;
}
export interface ChannelSyncState { checked_at?: string; changed_at?: string; change_summary?: string }
export interface RecipesStatus {
  enabled: boolean; paused: boolean; running: boolean;
  interval_hours: number; next_run_seconds: number | null;
  live_count: number; last_cycle: CycleSummary | null;
  channels: Record<string, ChannelSyncState>;
}
export interface ChannelsFile { channels: Channel[]; orphaned: string[]; suggested_channels: string[] }
export interface CsvInfo {
  exists: boolean;
  rows?: number;
  size?: number;
  modified?: number;
  preview?: string[];
  movies?: number;
  tv_shows?: number;
  skipped_movies?: number;
  skipped_shows?: number;
}
export interface ValidateResult { ok: boolean; count?: number; added?: number; skipped_dupes?: number; error?: string; channels?: Channel[] }

// ── Planner facets + prompt options ──
export interface GenreFacet { display: string; tag: string; count: number }
export interface DecadeFacet { label: string; start: number; end: number; count: number }
export interface GenreDecadeFacet { genre: string; display: string; decade_start: number; decade_label: string; count: number }
export interface BlendFacet { genres: string[]; displays: string[]; count: number }
export interface EntityFacet { value: string; count: number }
export interface TvGenreFacet { genre: string; count: number }
export interface MarathonFacet { title: string; episodes: number; seasons: number }
/** Genres present in BOTH the movie and TV libraries above the TV_MOVIE_MIX_MIN floor. */
export interface TvMovieGenreFacet { genre: string; tv_count: number; movie_count: number }
/** A TMDB-keyword themed channel candidate (computed from the F9 enrichment cache). */
export interface ThemeFacet { name: string; count: number; keyword_ids: number[]; titles: string[] }
export interface LibraryFacets {
  exists: boolean;
  movies?: number;
  tv_shows?: number;
  marathon_count?: number;
  min_items?: number;
  genres?: { canonical: GenreFacet[]; more: GenreFacet[] };
  decades?: DecadeFacet[];
  genre_decade?: GenreDecadeFacet[];
  blends?: BlendFacet[];
  studios?: EntityFacet[];
  directors?: EntityFacet[];
  actors?: EntityFacet[];
  tv_genres?: TvGenreFacet[];
  marathons?: MarathonFacet[];
  tv_movie_genres?: TvMovieGenreFacet[];
  /** TV show counts by TVmaze network, above NETWORK_MIN floor. Empty while the TVmaze scan is running. */
  networks?: EntityFacet[];
  /** TMDB-keyword themed channel candidates. Empty until the TMDB enrichment scan completes. */
  themes?: ThemeFacet[];
  /** Movie counts by Plex Country tag, above COUNTRY_MIN floor. Empty when column absent from CSV (old export). */
  countries?: EntityFacet[];
  /** Movie counts by Plex Mood tag, above MOOD_MIN floor. Empty when column absent. */
  moods?: EntityFacet[];
  /** Movie counts by Plex Style tag, above STYLE_MIN floor. Empty when column absent. */
  styles?: EntityFacet[];
}

// ── Planner v2 candidate composition ──
export type CandidateKind =
  | 'genre' | 'genre_decade' | 'blend' | 'studio' | 'director' | 'actor' | 'tv_genre' | 'marathon'
  | 'tv_movie_mix' | 'network' | 'programming_block' | 'franchise' | 'theme'
  | 'country' | 'mood' | 'style';
export interface CandidateSpec {
  kind: CandidateKind;
  name?: string;
  genre?: string;
  genres?: string[];
  decade_start?: number;
  value?: string;
  /** programming_block: the resolved member titles present in the library. */
  titles?: string[];
  shuffle?: 'ordered' | 'shuffle' | 'block';
  live?: boolean;
}

/** A single member of a franchise (present in the library). */
export interface FranchiseMember { title: string; year: number | null; type: string }
/** A franchise candidate from TMDB belongs_to_collection or Wikidata P179/P8345. */
export interface FranchiseCandidate {
  name: string;
  source: 'tmdb' | 'wikidata';
  members: FranchiseMember[];
}

/** A single programming block from the catalog, enriched with library-present members. */
export interface ProgrammingBlock {
  name: string;
  era: string;
  network: string;
  shows: string[];
  present_shows: string[];
  present_count: number;
}
export interface ComposeResult {
  ok: boolean;
  count: number;
  channels: { number: number; name: string; items: number }[];
  skipped: { name: string; reason: string }[];
}
export interface PromptOptions {
  target?: string;
  preferences?: string;
  start?: number;
  include_genres?: string[];
  exclude_genres?: string[];
  include_decades?: string[];
  exclude_decades?: string[];
  include_types?: string[];
  exclude_types?: string[];
}
export interface PlexLibrary { key: string; title: string; type: 'movie' | 'show'; server?: string }
export interface PlexCollection { id: string; name: string; count: number; section: string; summary: string; has_poster: boolean }
export interface CollectionSelection { name: string; channel_number: number; include: boolean }
export interface LogEntry { name: string; size: number; modified: number }

/** Persisted Planner intent saved to data/planner_state.json after each Build. */
export interface PlannerStateFile {
  activeGenres: Record<string, boolean>;
  activeDecades: Record<string, boolean>;
  selected: Record<string, CandidateSpec>;
  curate: Record<string, boolean>;
  // batch toggles
  aiExtras: boolean;
  commEnabled: boolean;
  commListId: string | null;
  commPad: string;
  autoUpdate: boolean;
}

/** A single channel entry in a deploy-preview bucket. */
export interface DeployPreviewChannel { number: number | null; name: string }
/** Result of POST /pipeline/deploy-preview — five named diff buckets. */
export interface DeployPreviewResult {
  create: DeployPreviewChannel[];
  update: DeployPreviewChannel[];
  delete: DeployPreviewChannel[];
  unchanged: DeployPreviewChannel[];
  foreign: DeployPreviewChannel[];
}

// ── SSE streaming ──────────────────────────────────────────────────────────────

export type StreamEvent =
  | { type: 'start'; cmd: string; log: string }
  | { type: 'line'; text: string }
  | { type: 'done'; returncode: number; log: string };

export async function streamPipeline(
  endpoint: string,
  params: Record<string, string> = {},
  onEvent: (e: StreamEvent) => void,
  body?: unknown,
): Promise<number> {
  const qs = new URLSearchParams(params).toString();
  const url = `${BASE}${endpoint}${qs ? `?${qs}` : ''}`;
  const fetchOpts: RequestInit = { method: 'POST' };
  if (body !== undefined) {
    fetchOpts.body = JSON.stringify(body);
    fetchOpts.headers = { 'Content-Type': 'application/json' };
  }
  const res = await fetch(url, fetchOpts);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  if (!res.body) throw new Error('No body');

  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let code = -1;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop() ?? '';
    for (const part of parts) {
      const line = part.startsWith('data: ') ? part.slice(6) : part;
      if (!line.trim()) continue;
      try {
        const ev = JSON.parse(line) as StreamEvent;
        onEvent(ev);
        if (ev.type === 'done') code = ev.returncode;
      } catch { /* ignore parse errors */ }
    }
  }
  return code;
}
