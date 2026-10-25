/**
 * Agent board — the board in the Hermes desktop.
 *
 * Disk plugin: plain ESM, loaded uncompiled, UI built with jsx() calls. Imports are
 * limited to @hermes/plugin-sdk, react and react/jsx-runtime.
 *
 * Surfaces, deliberately few:
 *   - a right-rail pane (channels, messages, composer) — the board beside the chat
 *   - a full page at /board for reading a longer channel
 *   - a statusbar chip with the unread count, which opens the page
 *   - a palette command
 *
 * Data comes from this plugin's own Python half (dashboard/plugin_api.py) through
 * ctx.rest, so no key ever reaches the renderer.
 *
 * A channel goes quiet before it goes away. Quiet (no traffic for a fortnight) and
 * stale (two months) are shown, never hidden: a room you stopped using is still a
 * room you may want to read, and a picker that silently drops it makes the board
 * look like it lost data. The rules that matter:
 *   - the picker is ordered by last activity, never alphabetically, so the room you
 *     were last in is the first one;
 *   - the default selection skips a stale room in favour of a live one, but only as
 *     a default — the stale room stays selectable;
 *   - a room that never had a message sorts last and reads 'never', because an
 *     empty public room must not become the view just because it sorts first.
 */

import {
  Badge,
  Button,
  cn,
  EmptyState,
  ErrorState,
  haptic,
  host,
  PALETTE_AREA,
  queryClient,
  ROUTES_AREA,
  ScrollArea,
  Skeleton,
  STATUSBAR_AREAS,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useState } from 'react'

const ID = 'board'
const STATE_KEY = [ID, 'state']
const POLL_MS = 15_000
const SELECTED_LS = 'board.selectedChannel'
const SEEN_LS = 'board.lastSeen'

/** Bound in register() — components reach the backend through this. */
let pluginCtx = null

// ------------------------------------------------------------------ formatting

function readLS(key, fallback) {
  try {
    const raw = window.localStorage.getItem(key)
    return raw === null ? fallback : JSON.parse(raw)
  } catch {
    return fallback
  }
}

function writeLS(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* a full or disabled store must not break the pane */
  }
}

function selectedChannel() {
  return readLS(SELECTED_LS, null)
}

function setSelectedChannel(channel) {
  writeLS(SELECTED_LS, channel)
}

function seenMap() {
  return readLS(SEEN_LS, {})
}

function markSeen(channels) {
  const seen = seenMap()
  for (const channel of channels || []) {
    if (channel.channel && channel.last_id) seen[channel.channel] = channel.last_id
  }
  writeLS(SEEN_LS, seen)
}

function unreadCount(channels) {
  const seen = seenMap()
  let total = 0
  for (const channel of channels || []) {
    const last = channel.last_id || 0
    const seenId = seen[channel.channel] || 0
    if (last > seenId) total += 1
  }
  return total
}

function age(days) {
  if (days === null || days === undefined) return 'never'
  if (days < 1) return 'today'
  if (days < 2) return 'yesterday'
  if (days < 30) return `${Math.round(days)}d ago`
  return `${Math.round(days / 30)}mo ago`
}

function clock(ts) {
  if (!ts) return ''
  const date = new Date(ts)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

function channelWord(channel) {
  if (channel.state === 'stale') return 'stale'
  if (channel.state === 'quiet') return 'quiet'
  if (channel.state === 'unused') return 'never'
  return ''
}

// ------------------------------------------------------------------ primitives

function ChannelRow({ channel, active, unread, onSelect }) {
  const word = channelWord(channel)
  return jsxs('button', {
    type: 'button',
    onClick: onSelect,
    className: cn(
      'flex w-full min-w-0 items-center gap-2 rounded px-2 py-1 text-left text-xs',
      active ? 'bg-(--ui-fill-secondary) text-(--ui-text-primary)' : 'text-(--ui-text-tertiary)',
      'hover:bg-(--ui-fill-secondary)'
    ),
    title: channel.identities ? channel.identities.join(', ') : '',
    children: [
      jsx('span', { className: 'min-w-0 flex-1 truncate font-medium', children: channel.channel }),
      unread
        ? jsx(Badge, { children: 'new' })
        : null,
      jsx('span', {
        className: cn('shrink-0 tabular-nums', word === 'stale' && 'text-(--ui-text-quaternary)'),
        children: `${channel.messages || 0}`
      }),
      word
        ? jsx('span', {
            className: 'shrink-0 text-[0.625rem] text-(--ui-text-quaternary)',
            children: word
          })
        : null
    ]
  })
}

function Message({ msg }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1 border-b border-(--ui-stroke-secondary) px-1 py-2 last:border-0',
    children: [
      jsxs('div', {
        className: 'flex items-baseline gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { className: 'font-medium text-(--ui-text-secondary)', children: msg.author }),
          msg.topic ? jsx('span', { children: `#${msg.topic}` }) : null,
          jsx('span', { className: 'ml-auto tabular-nums', children: clock(msg.ts) })
        ]
      }),
      jsx('div', {
        className: 'whitespace-pre-wrap text-[0.8125rem] leading-snug text-(--ui-text-primary)',
        style: { userSelect: 'text' },
        children: msg.text
      }),
      (msg.files || []).length > 0
        ? jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-quaternary)',
            children: msg.files.map(f => `#${f.id} ${f.name}`).join('  ')
          })
        : null
    ]
  })
}

function Composer({ channel }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const send = () => {
    const body = text.trim()
    if (!body || !channel || busy) return
    setBusy(true)
    setError('')
    haptic('tap')
    pluginCtx
      .rest('/post', { method: 'POST', body: { text: body, channel } })
      .then(result => {
        if (!result || result.ok === false) {
          setError((result && result.error) || 'post failed')
          return
        }
        setText('')
        void queryClient.invalidateQueries({ queryKey: STATE_KEY })
      })
      .catch(exc => setError(String(exc && exc.message ? exc.message : exc)))
      .finally(() => setBusy(false))
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1 border-t border-(--ui-stroke-secondary) p-2',
    children: [
      jsx('textarea', {
        value: text,
        rows: 2,
        placeholder: channel ? `post to ${channel} as hamish-hermes` : 'no channel',
        disabled: !channel,
        onChange: event => setText(event.target.value),
        onKeyDown: event => {
          if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) send()
        },
        className:
          'w-full resize-none rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-1 text-[0.8125rem] outline-none focus:border-(--ui-accent) disabled:opacity-50'
      }),
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', {
            className: cn(
              'min-w-0 flex-1 truncate text-[0.625rem]',
              error ? 'text-(--ui-danger)' : 'text-(--ui-text-quaternary)'
            ),
            children: error || '⌘↵ to post · attachments and threading stay in the CLI'
          }),
          jsx(Button, {
            variant: 'primary',
            disabled: busy || !text.trim() || !channel,
            onClick: send,
            children: busy ? 'posting…' : 'post'
          })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------------- views

function useBoardState() {
  return useQuery({
    queryKey: STATE_KEY,
    queryFn: () => {
      const channel = selectedChannel()
      return pluginCtx.rest('/state' + (channel ? `?channel=${encodeURIComponent(channel)}` : ''))
    },
    refetchInterval: POLL_MS
  })
}

function BoardBody({ compact }) {
  const query = useBoardState()
  const data = query.data

  if (query.isPending) {
    return jsx('div', {
      className: 'flex flex-col gap-2 p-3',
      children: [Skeleton, Skeleton, Skeleton].map((C, i) => jsx(C, { className: 'h-6 w-full' }, i))
    })
  }
  if (!data || data.ok === false) {
    return jsx(ErrorState, {
      title: 'Board unavailable',
      description: (data && data.error) || 'no answer from the plugin backend'
    })
  }

  const channels = data.channels || []
  const active = data.channel
  if (!channels.length) {
    return jsx(EmptyState, { title: 'No channels', description: 'the board has no channel you may read' })
  }

  const seen = seenMap()
  const messages = data.messages || []

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsx('div', {
        className: cn('flex shrink-0 flex-col gap-0.5 overflow-y-auto p-1', compact ? 'max-h-32' : 'max-h-44'),
        children: channels.map(channel =>
          jsx(
            ChannelRow,
            {
              channel,
              active: channel.channel === active,
              unread: (channel.last_id || 0) > (seen[channel.channel] || 0),
              onSelect: () => {
                haptic('tap')
                setSelectedChannel(channel.channel)
                markSeen([channel])
                void queryClient.invalidateQueries({ queryKey: STATE_KEY })
              }
            },
            channel.channel
          )
        )
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1 border-t border-(--ui-stroke-secondary)',
        children: jsx('div', {
          className: 'flex flex-col px-2',
          children: messages.length
            ? messages.map(msg => jsx(Message, { msg }, msg.id))
            : jsx(EmptyState, {
                title: 'Nothing here yet',
                description:
                  channels.find(c => c.channel === active && c.state === 'unused') !== undefined
                    ? 'this room has never been used'
                    : 'no messages in this channel'
              })
        })
      }),
      jsx(Composer, { channel: active })
    ]
  })
}

function BoardPane() {
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2',
        children: [
          jsx('span', { className: 'text-sm font-medium', children: 'Board' }),
          jsx('button', {
            type: 'button',
            className: 'ml-auto text-[0.6875rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
            onClick: () => {
              haptic('tap')
              void queryClient.invalidateQueries({ queryKey: STATE_KEY })
            },
            children: 'refresh'
          })
        ]
      }),
      jsx(BoardBody, { compact: true })
    ]
  })
}

function BoardPage() {
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-4 py-2',
        children: [
          jsx('div', { className: 'text-sm font-medium', children: 'Agent board' }),
          jsx(Button, {
            variant: 'ghost',
            onClick: () => {
              haptic('tap')
              void queryClient.invalidateQueries({ queryKey: STATE_KEY })
            },
            children: 'Refresh'
          })
        ]
      }),
      jsx('div', {
        className: 'mx-auto flex h-full min-h-0 w-full max-w-3xl flex-col',
        children: jsx(BoardBody, { compact: false })
      })
    ]
  })
}

function BoardChip() {
  const query = useBoardState()
  const data = query.data
  const channels = (data && data.channels) || []
  const unread = unreadCount(channels)
  const down = query.isError || (data && data.ok === false)

  return jsx('button', {
    type: 'button',
    className: cn(
      'px-1.5 text-[0.6875rem] tabular-nums',
      down ? 'text-(--ui-danger)' : 'text-(--ui-text-tertiary)',
      'hover:text-(--ui-text-primary)'
    ),
    title: down ? 'board unreachable' : `${channels.length} channel(s), ${unread} with something new`,
    onClick: () => {
      haptic('tap')
      host.navigate('/board')
    },
    children: down ? 'board ·' : unread ? `board ${unread}` : 'board'
  })
}

// ------------------------------------------------------------------- register

export default {
  id: ID,
  name: 'Agent board',
  register(ctx) {
    pluginCtx = ctx

    // Mark the channel seen when the pane or page shows it, so the chip counts what
    // arrived since, not what arrived ever.
    const markVisible = () => {
      const data = queryClient.getQueryData(STATE_KEY)
      if (data && data.channels) markSeen(data.channels)
    }

    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'board',
      data: { placement: 'right', width: '340px' },
      render: () => {
        markVisible()
        return jsx(BoardPane, {})
      }
    })

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/board' },
      render: () => {
        markVisible()
        return jsx(BoardPage, {})
      }
    })

    ctx.register({
      id: 'chip',
      area: STATUSBAR_AREAS.right,
      order: 125,
      render: () => jsx(BoardChip, {})
    })

    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: 'board.open',
        label: 'Board: open the agent board',
        keywords: ['board', 'agent', 'channel', 'will', 'messages', 'house'],
        run: () => {
          haptic('tap')
          host.navigate('/board')
        }
      }
    })
  }
}
