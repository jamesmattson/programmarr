import {
  ActionIcon, Badge, Box, Button, Card, Checkbox, Divider, Group,
  Loader, Modal, NumberInput, ScrollArea, Select, Stack, Switch, Text, TextInput, Title,
  Tooltip,
} from '@mantine/core';
import { useDisclosure } from '@mantine/hooks';
import { notifications } from '@mantine/notifications';
import {
  IconCheck, IconEdit, IconPlus, IconRepeat, IconTag, IconTrash, IconX,
} from '@tabler/icons-react';
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { AmbiguousChoice, api, Channel, ChannelNotice, ChannelReview, ChannelSyncState, ContentItem, FillerList, FranchiseRef, isMatchRef, LibraryMatch, RecipeMatch, TunarrChannel } from '../api/client';

function syncedAgo(iso?: string): string {
  if (!iso) return 'never';
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 90) return 'just now';
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

// What a channel's last update skipped or found again — shown on its row and in its editor,
// so an item that stops matching is never silently dropped.
function noticeText(n: Pick<ChannelReview, 'missing_count' | 'healed_count'>): string {
  const parts: string[] = [];
  if (n.missing_count) {
    parts.push(`${n.missing_count} item${n.missing_count === 1 ? '' : 's'} couldn't be found and ${n.missing_count === 1 ? 'was' : 'were'} skipped`);
  }
  if (n.healed_count) {
    parts.push(`${n.healed_count} ${n.healed_count === 1 ? 'was' : 'were'} found again through a backup number`);
  }
  return parts.join('; ');
}

// "1991" / "2017" — or "movie 2017" / "show 1975" when the choice mixes movies and shows.
function optionLabel(a: AmbiguousChoice, o: AmbiguousChoice['options'][number]): string {
  const mixed = new Set(a.options.map((x) => x.kind)).size > 1;
  const year = o.year ? String(o.year) : o.title;
  return mixed ? `${o.kind} ${year}` : year;
}

const SHUFFLE_COLOR: Record<string, string> = { ordered: 'blue', block: 'violet', shuffle: 'teal' };
const SHUFFLE_OPTIONS = [
  { value: 'shuffle',  label: 'Shuffle — random order' },
  { value: 'ordered',  label: 'Ordered — sequential' },
  { value: 'block',    label: 'Block — grouped by show' },
];

interface MatchRule { value: string; order: string; exclude: string[] }
const isFranchiseRef = (c: any): c is FranchiseRef =>
  typeof c === 'object' && c !== null && c.match === 'franchise';

// ── Franchise auto-match builder ───────────────────────────────────────────────

function FranchiseBuilder({
  initial,
  onSave,
  onCancel,
}: {
  initial: MatchRule | null;
  onSave: (rule: MatchRule) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial?.value ?? '');
  const [order, setOrder] = useState(initial?.order ?? 'release_date');
  const [excluded, setExcluded] = useState<Set<string>>(new Set(initial?.exclude ?? []));
  const [matches, setMatches] = useState<RecipeMatch[] | null>(null);
  const [previewing, setPreviewing] = useState(false);

  async function preview() {
    if (!value.trim()) return;
    setPreviewing(true);
    try {
      const res = await api.previewRecipe(value.trim(), order, []); // full candidate list
      setMatches(res.matches);
    } catch (e: any) {
      notifications.show({ title: 'Preview failed', message: e.message, color: 'red' });
    } finally {
      setPreviewing(false);
    }
  }

  // Auto-preview when editing an existing rule so the checklist is populated
  useEffect(() => { if (initial?.value) preview(); /* eslint-disable-next-line */ }, []);

  function toggle(title: string) {
    setExcluded((s) => {
      const n = new Set(s);
      if (n.has(title)) n.delete(title); else n.add(title);
      return n;
    });
  }

  const includedCount = matches ? matches.filter((m) => !excluded.has(m.title)).length : 0;

  return (
    <Card withBorder p="sm" style={{ background: 'var(--surface-sunken)' }}>
      <Stack gap="xs">
        <Text size="sm" fw={600}>Franchise auto-match</Text>
        <Text size="xs" c="dimmed">
          Auto-add every library title containing this phrase (whole-word match — "It" matches
          "It Follows", not "Little Women"). New matching films join the channel automatically.
        </Text>

        <Group gap="xs" align="end">
          <TextInput
            label="Title contains"
            placeholder="e.g. Bad Boys"
            value={value}
            onChange={(e) => setValue(e.currentTarget.value)}
            onKeyDown={(e) => e.key === 'Enter' && preview()}
            style={{ flex: 1 }}
            size="xs"
          />
          <Select
            label="Order"
            size="xs"
            w={140}
            data={[
              { value: 'release_date', label: 'Release date' },
              { value: 'alpha', label: 'Alphabetical' },
            ]}
            value={order}
            onChange={(v) => setOrder(v || 'release_date')}
            allowDeselect={false}
          />
          <Button size="xs" variant="light" color="orange" onClick={preview} loading={previewing}>
            Preview
          </Button>
        </Group>

        {matches && (
          <>
            <Text size="xs" c="dimmed">
              {includedCount} of {matches.length} included
              {excluded.size ? ` · ${excluded.size} excluded` : ''}
            </Text>
            {matches.length === 0 ? (
              <Text size="xs" c="yellow.4">No titles match — try a different phrase.</Text>
            ) : (
              <ScrollArea.Autosize mah={180}>
                <Stack gap={2}>
                  {matches.map((m) => (
                    <Checkbox
                      key={m.title}
                      size="xs"
                      color="orange"
                      checked={!excluded.has(m.title)}
                      onChange={() => toggle(m.title)}
                      label={
                        <Text size="xs">
                          {m.title}
                          {m.year ? <Text span c="dimmed"> ({m.year})</Text> : null}
                        </Text>
                      }
                    />
                  ))}
                </Stack>
              </ScrollArea.Autosize>
            )}
          </>
        )}

        <Group justify="flex-end" gap="xs">
          <Button size="xs" variant="subtle" color="gray" onClick={onCancel}>Cancel</Button>
          <Button
            size="xs"
            color="orange"
            disabled={!value.trim()}
            onClick={() => onSave({ value: value.trim(), order, exclude: Array.from(excluded) })}
          >
            Save rule
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}

// ── Channel editor modal ───────────────────────────────────────────────────────

// One row per saved entry. `raw` is the exact saved entry for anything that carries more
// than its text (a pinned item's ids, a collection), so editing and saving can never erase
// it. Rows typed into the box have no raw and are parsed on save, as before.
type ContentRow = { label: string; raw: ContentItem | null };

function rowFromEntry(c: ContentItem): ContentRow {
  if (typeof c === 'string') return { label: c, raw: null };
  const o = c as unknown as Record<string, unknown>;
  const [k, v] = Object.entries(o)[0];
  const shownYear = o.ids && o.year ? ` (${o.year})` : '';
  return { label: `{${k}: ${v}${shownYear}}`, raw: c };
}

function isPinned(row: ContentRow): boolean {
  return row.raw !== null && typeof row.raw === 'object' && 'ids' in row.raw;
}

function ChannelModal({
  channel,
  opened,
  onClose,
  onSaved,
}: {
  channel: Channel | null;
  opened: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState('');
  const [number, setNumber] = useState('');
  const [shuffle, setShuffle] = useState<string>('shuffle');
  const [content, setContent] = useState<ContentRow[]>([]);
  const [newItem, setNewItem] = useState('');
  const [looking, setLooking] = useState(false);
  // A live check of this channel against the library, fetched when the editor opens.
  const [review, setReview] = useState<ChannelReview | null>(null);
  const [choices, setChoices] = useState<{ text: string; matches: LibraryMatch[] } | null>(null);
  const [live, setLive] = useState(false);
  const [matchRef, setMatchRef] = useState<MatchRule | null>(null);
  const [franchiseRefs, setFranchiseRefs] = useState<FranchiseRef[]>([]);
  const [building, setBuilding] = useState(false);
  const [saving, setSaving] = useState(false);

  // Commercials
  const [commEnabled, setCommEnabled] = useState(false);
  const [commListId, setCommListId] = useState<string | null>(null);
  const [commPad, setCommPad] = useState('5');
  const [fillerLists, setFillerLists] = useState<FillerList[]>([]);

  // Playback structure
  const [pbStructure, setPbStructure] = useState<string>('default');
  const [pbEpisodes, setPbEpisodes] = useState<string | number>(4);

  // Channel icon
  const [iconBusy, setIconBusy] = useState<string | null>(null);
  const [iconUrl, setIconUrl] = useState('');
  const [customIconUrl, setCustomIconUrl] = useState('');

  // Filler lists live in Tunarr; fetch once so the picker can offer them.
  useEffect(() => {
    api.getFillerLists().then(setFillerLists).catch(() => setFillerLists([]));
  }, []);

  useEffect(() => {
    if (!channel) return;
    setName(channel.name);
    setNumber(String(channel.number));
    setShuffle(channel.shuffle || 'shuffle');
    setLive(!!channel.live);
    setBuilding(false);
    const comm = channel.commercials;
    setCommEnabled(!!comm?.filler_list_id);
    setCommListId(comm?.filler_list_id ?? null);
    setCommPad(String(comm?.pad_minutes ?? 5));

    setIconUrl(channel.icon?.url ?? '');
    setCustomIconUrl('');

    setPbStructure(channel.playback?.structure ?? 'default');
    setPbEpisodes(channel.playback?.episodes_per_block ?? 4);

    const mref = channel.content.find(isMatchRef);
    setMatchRef(mref ? { value: mref.value, order: mref.order || 'release_date', exclude: mref.exclude || [] } : null);
    setFranchiseRefs(channel.content.filter(isFranchiseRef));
    setChoices(null);
    setContent(
      channel.content
        .filter((c) => !isMatchRef(c) && !isFranchiseRef(c))
        .map(rowFromEntry)
    );
  }, [channel]);

  useEffect(() => {
    setReview(null);
    if (!channel || !opened) return;
    let alive = true;
    api.getChannelReview(channel.number).then((r) => { if (alive) setReview(r); }).catch(() => {});
    return () => { alive = false; };
  }, [channel, opened]);

  // Swap a plain title for the exact item(s) the person picked. Nothing is saved until
  // Save and Apply, like every other edit here.
  function choose(a: AmbiguousChoice, options: AmbiguousChoice['options']) {
    const want = a.label.toLowerCase().trim();
    const isTarget = (row: ContentRow) => {
      if (row.raw === null) return row.label.toLowerCase().trim() === want;
      const o = row.raw as unknown as Record<string, unknown>;
      const title = o.movie ?? o.show;
      return typeof title === 'string' && !o.ids && title.toLowerCase().trim() === want;
    };
    setContent((rows) => {
      const out: ContentRow[] = [];
      let placed = false;
      for (const r of rows) {
        if (!isTarget(r)) { out.push(r); continue; }
        if (!placed) { options.forEach((o) => out.push(rowFromEntry(o.entry))); placed = true; }
      }
      return out;
    });
    setReview((r) => (r ? {
      ...r, ambiguous: r.ambiguous.filter((x) => x !== a), ambiguous_count: Math.max(0, r.ambiguous_count - 1),
    } : r));
  }

  function pushRow(row: ContentRow) {
    setContent((c) => [...c, row]);
    setNewItem('');
    setChoices(null);
  }

  // Look the title up in the library first, so what gets saved is the exact item:
  //   one match  -> saved with the item's own numbers;
  //   several    -> ask which one (the two Aladdins);
  //   none       -> added as typed, exactly like before.
  async function addItem() {
    const text = newItem.trim();
    if (!text || looking) return;
    const typed = text.match(/^\{(collection|movie|show):\s*(.+)\}$/);
    // A collection stays a live reference — never frozen into whatever it holds today.
    if (typed && typed[1] === 'collection') return pushRow({ label: text, raw: null });
    const kind = typed ? (typed[1] as 'movie' | 'show') : undefined;
    const title = typed ? typed[2].trim() : text;
    setLooking(true);
    try {
      const { matches } = await api.lookupLibrary(title, kind);
      if (matches.length === 1) return pushRow(rowFromEntry(matches[0].entry));
      if (matches.length > 1) return setChoices({ text, matches });
      notifications.show({ color: 'yellow', message: `"${title}" isn't in your Tunarr library — added as typed.` });
    } catch {
      // Tunarr unreachable or not configured: keep the old behaviour instead of blocking the add.
    } finally {
      setLooking(false);
    }
    pushRow({ label: text, raw: null });
  }

  function removeItem(i: number) {
    setContent((c) => c.filter((_, idx) => idx !== i));
  }

  async function persist() {
    const rawContent: ContentItem[] = content.map((r) => {
      if (r.raw) return r.raw;
      const m = r.label.match(/^\{(collection|movie|show):\s*(.+)\}$/);
      return m ? ({ [m[1]]: m[2].trim() } as ContentItem) : r.label;
    });
    if (matchRef) {
      rawContent.push({
        match: 'title_contains',
        value: matchRef.value,
        order: matchRef.order,
        exclude: matchRef.exclude,
      });
    }
    franchiseRefs.forEach((ref) => rawContent.push(ref));
    const payload: any = { number: Number(number), name, shuffle, content: rawContent };
    if (live) payload.live = true;
    if (commEnabled && commListId) {
      payload.commercials = {
        filler_list_id: commListId,
        filler_list_name: fillerLists.find((f) => f.id === commListId)?.name,
        pad_minutes: Number(commPad),
      };
    }
    if (pbStructure === 'interleaved') {
      payload.playback = { structure: 'interleaved', episodes_per_block: Number(pbEpisodes) || 4 };
    } else if (pbStructure === 'timeline') {
      payload.playback = { structure: 'timeline' };
    }
    // 'default' → no playback key at all
    await api.updateChannel(channel!.number, payload);
  }

  // Save to channels.json then immediately push to Tunarr in place.
  async function saveAndApply() {
    if (!channel) return;
    setSaving(true);
    try {
      await persist();
      const { notice: n } = await api.applyChannel(channel.number);
      if (n && n.missing_count > 0) {
        notifications.show({
          color: 'yellow', autoClose: 15000,
          title: `Channel #${channel.number} applied — ${n.missing_count} item${n.missing_count === 1 ? '' : 's'} skipped`,
          message: n.missing.slice(0, 5).map((m) => `${m.label} (${m.why})`).join('; ')
            + (n.missing_count > 5 ? `; …and ${n.missing_count - 5} more` : ''),
        });
      } else {
        notifications.show({
          message: `Channel #${channel.number} saved and applied` + (n?.healed_count ? ` (${noticeText(n)})` : ''),
          color: 'green', icon: <IconCheck size={14} />,
        });
      }
      if (n?.ambiguous_count) {
        notifications.show({
          color: 'blue', autoClose: 10000,
          message: `${n.ambiguous_count} title${n.ambiguous_count === 1 ? '' : 's'} in channel #${channel.number} still match more than one item — open it to choose which.`,
        });
      }
      onSaved();
      onClose();
    } catch (e: any) {
      notifications.show({ title: 'Error', message: e.message, color: 'red' });
    } finally {
      setSaving(false);
    }
  }

  async function applyIcon(mode: 'badge' | 'tmdb' | 'custom' | 'clear') {
    if (!channel) return;
    setIconBusy(mode);
    try {
      const res = await api.setChannelIcon(
        channel.number,
        mode === 'custom' ? { mode, url: customIconUrl.trim() } : { mode },
      );
      setIconUrl(res.url);
      notifications.show({
        message: mode === 'clear' ? 'Icon reset to automatic' : 'Channel icon updated',
        color: 'green',
      });
    } catch (e: any) {
      notifications.show({ title: 'Icon update failed', message: e.message, color: 'red' });
    } finally {
      setIconBusy(null);
    }
  }

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={<Text fw={700}>Edit Channel #{channel?.number}</Text>}
      size="lg"
    >
      <Stack gap="sm">
        <Group grow>
          <TextInput
            label="Channel number"
            value={number}
            onChange={(e) => setNumber(e.currentTarget.value)}
          />
          <TextInput
            label="Name"
            value={name}
            onChange={(e) => setName(e.currentTarget.value)}
          />
        </Group>

        <Select
          label="Shuffle mode"
          data={SHUFFLE_OPTIONS}
          value={shuffle}
          onChange={(v) => setShuffle(v || 'shuffle')}
        />

        <Divider label="Content" labelPosition="left" />

        {review && (review.missing_count > 0 || review.healed_count > 0) && (
          <Card withBorder p="xs" style={{ background: 'var(--surface-panel)' }}>
            <Text size="xs" fw={600} c="yellow" mb={4}>⚠ Right now: {noticeText(review)}.</Text>
            {review.missing.map((m, i) => (
              <Text key={i} size="xs" style={{ fontFamily: 'ui-monospace, monospace' }}>
                {m.label} <Text span c="dimmed">— {m.why}</Text>
              </Text>
            ))}
            {review.missing_count > review.missing.length && (
              <Text size="xs" c="dimmed">…and {review.missing_count - review.missing.length} more</Text>
            )}
            {review.missing.length > 0 && (
              <Text size="xs" c="dimmed" mt={4}>
                To fix one: remove it below (✕) and add it again — the Add box offers the right match.
              </Text>
            )}
          </Card>
        )}

        {review && review.ambiguous.length > 0 && (
          <Card withBorder p="xs" style={{ background: 'var(--surface-panel)' }}>
            <Text size="xs" fw={600} c="orange" mb={4}>
              {review.ambiguous_count} title{review.ambiguous_count === 1 ? '' : 's'} match more than one item — pick which to play:
            </Text>
            <Stack gap={6} style={{ maxHeight: 280, overflowY: 'auto' }}>
              {review.ambiguous.map((a) => (
                <Group key={`${a.label}|${a.kind ?? ''}`} gap={6}>
                  <Text size="xs" style={{ fontFamily: 'ui-monospace, monospace' }}>{a.label}</Text>
                  {a.options.map((o, i) => (
                    <Button key={i} size="compact-xs" variant="light" color="orange" onClick={() => choose(a, [o])}>
                      {optionLabel(a, o)}
                    </Button>
                  ))}
                  <Button size="compact-xs" variant="subtle" color="gray" onClick={() => choose(a, a.options)}>
                    Both
                  </Button>
                  {a.current !== null && a.options[a.current] && (
                    <Text size="xs" c="dimmed">plays now: {optionLabel(a, a.options[a.current])}</Text>
                  )}
                </Group>
              ))}
            </Stack>
            <Text size="xs" c="dimmed" mt={4}>Your choices take effect after Save and Apply.</Text>
          </Card>
        )}

        <Stack gap={4}>
          {content.map((row, i) => (
            <Group key={i} gap="xs" wrap="nowrap">
              <Text size="sm" style={{ flex: 1, fontFamily: 'ui-monospace, monospace' }} truncate>
                {row.label}
                {isPinned(row) && <Text span c="dimmed" size="xs"> · pinned</Text>}
              </Text>
              <ActionIcon size="sm" color="red" variant="subtle" onClick={() => removeItem(i)}>
                <IconX size={14} />
              </ActionIcon>
            </Group>
          ))}
          {content.length === 0 && !matchRef && franchiseRefs.length === 0 && (
            <Text size="xs" c="dimmed">No fixed titles — add titles below or a franchise auto-match.</Text>
          )}
          {franchiseRefs.map((r) => (
            <Text key={r.name} size="sm" c="dimmed">
              🔁 Franchise: {r.name} (auto-updating{r.exclude?.length ? `, ${r.exclude.length} excluded` : ''})
            </Text>
          ))}
        </Stack>

        <Group gap="xs">
          <TextInput
            placeholder="Add title, {movie: Title}, {show: Title} or {collection: Name}"
            value={newItem}
            onChange={(e) => setNewItem(e.currentTarget.value)}
            onKeyDown={(e) => e.key === 'Enter' && addItem()}
            style={{ flex: 1 }}
            size="sm"
          />
          <Button size="sm" variant="light" color="orange" onClick={addItem} loading={looking}
            leftSection={<IconPlus size={14} />}>
            Add
          </Button>
        </Group>

        {choices && (
          <Card withBorder p="xs" style={{ background: 'var(--surface-panel)' }}>
            <Text size="xs" fw={600} mb={4}>
              "{choices.text}" matches {choices.matches.length} items in your library — which one?
            </Text>
            <Stack gap={4}>
              {choices.matches.map((m, i) => (
                <Button key={i} size="compact-sm" variant="light" color="orange" justify="flex-start"
                  onClick={() => pushRow(rowFromEntry(m.entry))}>
                  {m.title}{m.year ? ` (${m.year})` : ''} — {m.kind}
                </Button>
              ))}
              <Button size="compact-xs" variant="subtle" color="gray" onClick={() => setChoices(null)}>
                Cancel
              </Button>
            </Stack>
          </Card>
        )}

        <Divider label="Live recipe" labelPosition="left" />

        <Switch
          checked={live}
          onChange={(e) => setLive(e.currentTarget.checked)}
          color="orange"
          label="Auto-update on a schedule"
          description="Re-resolves this channel against your library and patches it in place. New episodes and matching franchise films appear automatically — no redeploy."
        />

        {matchRef && !building ? (
          <Card withBorder p="xs">
            <Group justify="space-between" wrap="nowrap">
              <Box style={{ minWidth: 0 }}>
                <Group gap={6}>
                  <IconRepeat size={14} />
                  <Text size="sm" fw={600} truncate>“{matchRef.value}”</Text>
                </Group>
                <Text size="xs" c="dimmed">
                  {matchRef.order === 'release_date' ? 'release date order' : 'alphabetical'}
                  {matchRef.exclude.length ? ` · ${matchRef.exclude.length} excluded` : ''}
                </Text>
              </Box>
              <Group gap={4}>
                <ActionIcon variant="subtle" color="gray" onClick={() => setBuilding(true)}>
                  <IconEdit size={14} />
                </ActionIcon>
                <ActionIcon variant="subtle" color="red" onClick={() => setMatchRef(null)}>
                  <IconTrash size={14} />
                </ActionIcon>
              </Group>
            </Group>
          </Card>
        ) : building ? (
          <FranchiseBuilder
            initial={matchRef}
            onSave={(rule) => { setMatchRef(rule); setBuilding(false); }}
            onCancel={() => setBuilding(false)}
          />
        ) : (
          <Button
            size="xs"
            variant="light"
            color="orange"
            leftSection={<IconPlus size={14} />}
            onClick={() => setBuilding(true)}
            style={{ alignSelf: 'flex-start' }}
          >
            Add franchise auto-match
          </Button>
        )}

        <Divider label="Commercials" labelPosition="left" />

        <Switch
          checked={commEnabled}
          onChange={(e) => {
            const on = e.currentTarget.checked;
            setCommEnabled(on);
            if (on && !commListId && fillerLists.length) setCommListId(fillerLists[0].id);
          }}
          color="orange"
          label="Play commercials between shows"
          description="Pulls clips from a Tunarr filler list and plays them in a short gap after each show — like real TV."
        />

        {commEnabled && (
          fillerLists.length === 0 ? (
            <Text size="xs" c="yellow.4">
              No filler lists found in Tunarr. Create one in Tunarr first (a library of
              commercial / bumper clips), then reopen this editor.
            </Text>
          ) : (
            <Group grow align="start">
              <Select
                label="Filler list"
                data={fillerLists.map((f) => ({ value: f.id, label: `${f.name} (${f.contentCount})` }))}
                value={commListId}
                onChange={setCommListId}
                allowDeselect={false}
              />
              <Select
                label="Break length"
                data={[
                  { value: '5', label: 'Short (~3 min)' },
                  { value: '30', label: 'Long (~8 min)' },
                ]}
                value={commPad}
                onChange={(v) => setCommPad(v || '5')}
                allowDeselect={false}
              />
            </Group>
          )
        )}

        <Divider label="Playback structure" labelPosition="left" />
        <Select
          size="xs"
          value={pbStructure}
          onChange={(v) => setPbStructure(v ?? 'default')}
          data={[
            { value: 'default', label: 'Standard (use shuffle setting)' },
            { value: 'interleaved', label: 'Interleaved — movies in order, episode blocks between' },
            { value: 'timeline', label: 'Timeline — strict release order' },
          ]}
        />
        {pbStructure === 'interleaved' && (
          <NumberInput size="xs" label="Episodes per block" min={1} max={12}
                       value={pbEpisodes} onChange={setPbEpisodes} />
        )}
        {pbStructure === 'timeline' && (
          <Text size="xs" c="dimmed">Commercial padding is not applied in timeline mode.</Text>
        )}

        {channel && (
          <>
            <Divider label="Channel icon" labelPosition="left" />
            {iconUrl && (
              <img src={iconUrl} alt="channel icon"
                   style={{ height: 48, width: 48, objectFit: 'contain', alignSelf: 'flex-start' }} />
            )}
            <Group gap="xs">
              <Button size="xs" variant="light" loading={iconBusy === 'badge'}
                      onClick={() => applyIcon('badge')}>
                Use badge
              </Button>
              <Button size="xs" variant="light" loading={iconBusy === 'tmdb'}
                      onClick={() => applyIcon('tmdb')}>
                Re-fetch TMDB logo
              </Button>
              <Button size="xs" variant="subtle" color="gray" loading={iconBusy === 'clear'}
                      onClick={() => applyIcon('clear')}>
                Reset to automatic
              </Button>
            </Group>
            <Group gap="xs">
              <TextInput size="xs" placeholder="https://… custom icon URL"
                         value={customIconUrl} style={{ flex: 1 }}
                         onChange={(e) => setCustomIconUrl(e.currentTarget.value)} />
              <Button size="xs" variant="light" disabled={!customIconUrl.trim()}
                      loading={iconBusy === 'custom'} onClick={() => applyIcon('custom')}>
                Set
              </Button>
            </Group>
            <Text size="xs" c="dimmed">
              Choosing an icon pins it — automatic art passes skip this channel. "Reset to
              automatic" unpins it.
            </Text>
          </>
        )}

        <Divider />

        <Group justify="flex-end">
          <Button variant="subtle" color="gray" onClick={onClose}>Cancel</Button>
          <Button color="orange" onClick={saveAndApply} loading={saving}>Save and Apply</Button>
        </Group>
      </Stack>
    </Modal>
  );
}

// ── Channel row ────────────────────────────────────────────────────────────────

function ChannelRow({
  channel,
  sync,
  notice,
  onEdit,
  onDelete,
}: {
  channel: TunarrChannel;
  sync?: ChannelSyncState;
  notice?: ChannelNotice | ChannelReview;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [deleting, setDeleting] = useState(false);

  async function del() {
    if (!confirm(`Delete channel #${channel.number} "${channel.name}"?`)) return;
    setDeleting(true);
    try {
      await api.deleteChannel(channel.number);
      onDelete();
    } catch (e: any) {
      notifications.show({ title: 'Error', message: e.message, color: 'red' });
      setDeleting(false);
    }
  }

  return (
    <Card p="sm" mb="xs">
      <Group gap="sm" wrap="nowrap">
        <Badge
          size="lg"
          variant="filled"
          color="dark"
          radius="sm"
          style={{ minWidth: 52, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}
        >
          {channel.number}
        </Badge>

        <Box style={{ flex: 1, minWidth: 0 }}>
          <Text size="sm" fw={600} truncate>{channel.name}</Text>
          {sync?.checked_at && (
            <Group gap={4} mt={2}>
              <Badge size="xs" color="orange" variant="light" leftSection={<IconRepeat size={10} />}>
                live
              </Badge>
              <Text size="xs" c="dimmed">synced {syncedAgo(sync.checked_at)}</Text>
            </Group>
          )}
          {notice && (notice.ambiguous_count ?? 0) > 0 && (
            <Tooltip
              multiline w={340}
              label={
                <Stack gap={2}>
                  <Text size="xs">Match more than one item — open the channel to choose:</Text>
                  {(notice.ambiguous ?? []).slice(0, 8).map((a, i) => (
                    <Text key={i} size="xs">{a.label}</Text>
                  ))}
                  {(notice.ambiguous_count ?? 0) > 8 && <Text size="xs">…and {(notice.ambiguous_count ?? 0) - 8} more</Text>}
                </Stack>
              }
            >
              <Badge size="xs" color="blue" variant="light" mt={2} mr={4} style={{ cursor: 'default' }}>
                {notice.ambiguous_count} to review
              </Badge>
            </Tooltip>
          )}
          {notice && notice.missing_count > 0 && (
            <Tooltip
              multiline w={340}
              label={
                <Stack gap={2}>
                  {notice.missing.slice(0, 8).map((m, i) => (
                    <Text key={i} size="xs">{m.label} — {m.why}</Text>
                  ))}
                  {notice.missing_count > 8 && <Text size="xs">…and {notice.missing_count - 8} more</Text>}
                </Stack>
              }
            >
              <Badge size="xs" color="yellow" variant="light" mt={2} style={{ cursor: 'default' }}>
                ⚠ {notice.missing_count} skipped
              </Badge>
            </Tooltip>
          )}
        </Box>

        <Group gap={4} style={{ flexShrink: 0 }}>
          <Tooltip label="Edit">
            <ActionIcon variant="subtle" color="gray" onClick={onEdit}>
              <IconEdit size={16} />
            </ActionIcon>
          </Tooltip>
          <Tooltip label="Delete from channels.json">
            <ActionIcon variant="subtle" color="red" onClick={del} loading={deleting}>
              <IconTrash size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>
    </Card>
  );
}

function OrphanRow({ channel }: { channel: TunarrChannel }) {
  return (
    <Card p="sm" mb="xs" style={{ opacity: 0.65 }}>
      <Group gap="sm" wrap="nowrap">
        <Badge
          size="lg"
          variant="filled"
          color="dark"
          radius="sm"
          style={{ minWidth: 52, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}
        >
          {channel.number}
        </Badge>

        <Box style={{ flex: 1, minWidth: 0 }}>
          <Text size="sm" fw={600} truncate>{channel.name}</Text>
          <Badge size="xs" color="gray" variant="light" mt={2}>Not managed by Programmarr</Badge>
        </Box>
      </Group>
    </Card>
  );
}

// ── Root ───────────────────────────────────────────────────────────────────────

export default function Channels() {
  const { number } = useParams<{ number?: string }>();
  const nav = useNavigate();
  const [channels, setChannels] = useState<TunarrChannel[]>([]);
  const [managed, setManaged] = useState<Set<number>>(new Set());
  const [sync, setSync] = useState<Record<string, ChannelSyncState>>({});
  const [notices, setNotices] = useState<Record<string, ChannelNotice>>({});
  // A live check of every channel: what the saved notices know is only what the last Apply or
  // auto-update saw, so a channel nobody has applied since would otherwise show nothing here.
  const [reviews, setReviews] = useState<Record<string, ChannelReview>>({});
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<Channel | null>(null);
  const [opened, { open, close }] = useDisclosure(false);

  async function load() {
    const [tunarr, local] = await Promise.all([
      api.getTunarrChannels(),
      api.getChannels().catch(() => ({ channels: [], orphaned: [], suggested_channels: [] })),
    ]);
    setChannels([...tunarr].sort((a, b) => a.number - b.number));
    setManaged(new Set(local.channels.map((c) => c.number)));
    setLoading(false);
    api.getRecipesStatus().then((s) => setSync(s.channels || {})).catch(() => {});
    api.getChannelNotices().then(setNotices).catch(() => {});
    api.getChannelReviews().then(setReviews).catch(() => {});
  }

  useEffect(() => { load(); }, []);

  // Deep-link: open the channel named in the URL once the list has loaded.
  // The ref guard ensures a list reload (e.g. after Save) never re-opens a
  // modal the user just closed — fixing the "save bounces the window" bug.
  const openedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!number) { openedFor.current = null; return; }
    if (openedFor.current === number || channels.length === 0) return;
    const n = Number(number);
    if (!managed.has(n)) return; // orphan — don't open modal
    openedFor.current = number;
    api.getChannel(n)
      .then((ch) => { setEditing(ch); open(); })
      .catch(() => {}); // 404 = orphan after all; stay closed
  }, [number, channels, managed]);

  function edit(ch: TunarrChannel) {
    nav(`/channels/${ch.number}`, { replace: true });
    api.getChannel(ch.number)
      .then((full) => { setEditing(full); open(); })
      .catch(() => {});
  }

  function handleClose() {
    close();
    nav('/channels', { replace: true });
  }

  if (loading) {
    return <Stack align="center" justify="center" h={400}><Loader color="orange" /></Stack>;
  }

  const liveCount = Array.from(managed).filter((n) => {
    // live flag only known from channels.json — approximate from sync state
    return !!sync[String(n)];
  }).length;
  // Show live badge count from sync state as a proxy; exact count needs local data
  const syncedCount = Object.keys(sync).length;

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <Title order={2}>Channels ({channels.length})</Title>
        {syncedCount > 0 && (
          <Badge color="orange" variant="light" leftSection={<IconRepeat size={12} />}>
            {syncedCount} live
          </Badge>
        )}
      </Group>

      {channels.length === 0 ? (
        <Card p="xl">
          <Stack align="center" gap="xs">
            <Text c="dimmed">No channels in Tunarr yet — run the pipeline first</Text>
          </Stack>
        </Card>
      ) : (
        <Box>
          {channels.map((ch) => (
            managed.has(ch.number) ? (
              <ChannelRow
                key={ch.number}
                channel={ch}
                sync={sync[String(ch.number)]}
                notice={reviews[String(ch.number)] ?? notices[String(ch.number)]}
                onEdit={() => edit(ch)}
                onDelete={() => load()}
              />
            ) : (
              <OrphanRow key={ch.number} channel={ch} />
            )
          ))}
        </Box>
      )}

      <ChannelModal
        channel={editing}
        opened={opened}
        onClose={handleClose}
        onSaved={load}
      />
    </Stack>
  );
}
