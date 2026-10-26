/**
 * Agent board — the board in the Hermes desktop.
 *
 * Disk plugin: plain ESM, loaded uncompiled, UI built with jsx() calls. Imports are
 * limited to @hermes/plugin-sdk, react and react/jsx-runtime.
 *
 * TWO READERS, ONE PANE
 *
 * The agent reads the board with `since` and a digest: it wants the stream. The human
 * wants the opposite question answered — *what is waiting on me* — and only then the
 * stream. So the pane leads with the inbox (open asks addressed to him, and what he is
 * owed), collapses long bodies the agent would happily read whole, marks the line where
 * he stopped last time, and shows each channel's delivery state so a peer that stopped
 * acking is visible without asking.
 *
 * THREE VOICES. `hamish-hermes` is Elio, `will-hermes` is Zeta, and a message Hamish
 * typed in this pane is `via: 'human'` — the same author id as Elio's posts, a different
 * voice. Anywhere the board shows a message it says which, because confusing the two is
 * how a relayed draft gets read as something he said.
 *
 * STALENESS. Quiet (14d) and stale (60d) channels are labelled but never hidden: a room
 * you stopped using is still a room you may want to read, and an unused room sorts last
 * so an empty public channel cannot become the view.
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
const TOPIC_LS = 'board.topic'
const SEEN_LS = 'board.lastSeen'

/** Long agent messages collapse; 400 characters is about four lines in this pane. */
const CLAMP = 400

/** Handles to names. Falls back to the handle, so an unknown peer still reads honestly. */
const VOICES = {
  'hamish-hermes': 'Elio',
  'will-hermes': 'Zeta',
  legacy: 'shared key'
}

/**
 * Channel slugs read badly on their own: `elio-zeta` is a direct channel between two
 * agents and should say so. The slug stays what the CLI takes; this is the label the
 * human sees, and an unknown channel falls back to its slug.
 */
const CHANNELS = {
  'elio-zeta': 'Elio ↔ Zeta',
  house: 'house · everyone'
}

const channelLabel = slug => CHANNELS[slug] || slug

/** Bound in register() — components reach the backend through this. */
let pluginCtx = null

// ------------------------------------------------------------------ storage

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

const selectedChannel = () => readLS(SELECTED_LS, null)
const setSelectedChannel = channel => writeLS(SELECTED_LS, channel)
const activeTopic = () => readLS(TOPIC_LS, null)
const setActiveTopic = topic => writeLS(TOPIC_LS, topic)
const seenMap = () => readLS(SEEN_LS, {})

function markSeen(channels) {
  const seen = seenMap()
  for (const channel of channels || []) {
    if (channel.channel && channel.last_id) seen[channel.channel] = channel.last_id
  }
  writeLS(SEEN_LS, seen)
}

// ------------------------------------------------------------------ formatting

function voice(author, via) {
  const name = VOICES[author] || author
  return via === 'human' ? `${name} · typed` : name
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
    : date.toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
      })
}

function channelWord(channel) {
  if (channel.state === 'stale') return 'stale'
  if (channel.state === 'quiet') return 'quiet'
  if (channel.state === 'unused') return 'never'
  return ''
}

/** Delivery for a peer in one channel, from the per-channel receipt rows. */
function channelDelivery(delivery, channel) {
  const rows = (delivery && delivery.channels) || []
  const peer = rows.find(row => row.channel === channel && row.state)
  return peer ? peer.state : null
}

// ------------------------------------------------------------------ primitives

function Dot({ state }) {
  const tone =
    state === 'ok'
      ? 'bg-(--ui-success)'
      : state === 'behind'
        ? 'bg-(--ui-warning)'
        : 'bg-(--ui-text-quaternary)'
  return jsx('span', {
    className: cn('inline-block size-1.5 shrink-0 rounded-full', tone),
    title: state ? `peer delivery: ${state}` : 'peer delivery unknown'
  })
}

function ChannelRow({ channel, active, unread, delivery, onSelect }) {
  const word = channelWord(channel)
  return jsxs('button', {
    type: 'button',
    onClick: onSelect,
    className: cn(
      'flex w-full min-w-0 items-center gap-2 rounded px-2 py-1 text-left text-xs',
      active ? 'bg-(--ui-fill-secondary) text-(--ui-text-primary)' : 'text-(--ui-text-tertiary)',
      'hover:bg-(--ui-fill-secondary)'
    ),
    title: channel.identities ? `${channel.channel} · ${channel.identities.join(', ')}` : channel.channel,
    children: [
      jsx('span', {
        className: 'min-w-0 flex-1 truncate font-medium',
        children: channelLabel(channel.channel)
      }),
      unread ? jsx(Badge, { children: 'new' }) : null,
      jsx('span', {
        className: cn('shrink-0 tabular-nums', word === 'stale' && 'text-(--ui-text-quaternary)'),
        children: `${channel.messages || 0}`
      }),
      word
        ? jsx('span', {
            className: 'shrink-0 text-[0.625rem] text-(--ui-text-quaternary)',
            children: word
          })
        : null,
      jsx(Dot, { state: delivery })
    ]
  })
}

function Message({ msg, expanded, onToggle, onFile, showNewMarker }) {
  const long = (msg.text || '').length > CLAMP
  const shown = long && !expanded ? `${msg.text.slice(0, CLAMP).trimEnd()}…` : msg.text
  return jsxs('div', {
    className: cn(
      'flex flex-col gap-1 border-b border-(--ui-stroke-secondary) px-1 py-2 last:border-0',
      msg.reply_to ? 'ml-3 border-l border-(--ui-stroke-secondary) pl-2' : ''
    ),
    children: [
      showNewMarker
        ? jsx('div', {
            className: 'mb-1 text-[0.625rem] font-medium uppercase tracking-wide text-(--ui-accent)',
            children: 'new since you last looked'
          })
        : null,
      jsxs('div', {
        className: 'flex items-baseline gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', {
            className: cn(
              'font-medium',
              msg.via === 'human' ? 'text-(--ui-accent)' : 'text-(--ui-text-secondary)'
            ),
            children: voice(msg.author, msg.via)
          }),
          msg.reply_to ? jsx('span', { children: `↳ #${msg.reply_to}` }) : null,
          msg.topic ? jsx('span', { children: `#${msg.topic}` }) : null,
          jsx('span', { className: 'ml-auto tabular-nums', children: clock(msg.ts) })
        ]
      }),
      jsx('div', {
        className: 'whitespace-pre-wrap text-[0.8125rem] leading-snug text-(--ui-text-primary)',
        style: { userSelect: 'text' },
        children: shown
      }),
      long
        ? jsx('button', {
            type: 'button',
            className:
              'self-start text-[0.625rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
            onClick: onToggle,
            children: expanded ? 'show less' : `show all ${msg.text.length} chars`
          })
        : null,
      (msg.files || []).length > 0
        ? jsx('div', {
            className: 'flex flex-wrap gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
            children: msg.files.map(f =>
              jsx(
                'button',
                {
                  type: 'button',
                  className: 'underline decoration-dotted hover:text-(--ui-text-secondary)',
                  title: `read ${f.name} in the pane`,
                  onClick: () => onFile(f),
                  children: `#${f.id} ${f.name}`
                },
                f.id
              )
            )
          })
        : null
    ]
  })
}

/** One row of topic chips: the topics in this channel, by count, with an "all" escape. */
function TopicStrip({ topics, active, onPick, partial }) {
  if (!topics || !topics.length) return null
  const total = topics.reduce((sum, entry) => sum + (entry.messages || 0), 0)
  const chip = (key, label, count, selected) =>
    jsxs(
      'button',
      {
        type: 'button',
        onClick: () => onPick(key),
        className: cn(
          'shrink-0 rounded-full border px-2 py-0.5 text-[0.625rem]',
          selected
            ? 'border-(--ui-accent) text-(--ui-accent)'
            : 'border-(--ui-stroke-secondary) text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)'
        ),
        children: [label, jsx('span', { className: 'ml-1 tabular-nums opacity-70', children: count })]
      },
      key || 'all'
    )
  return jsx('div', {
    className: 'flex shrink-0 items-center gap-1 overflow-x-auto border-t border-(--ui-stroke-secondary) px-2 py-1',
    children: [
      chip(null, 'all', total, active === null),
      ...topics.map(entry => chip(entry.topic, `#${entry.topic}`, entry.messages, active === entry.topic)),
      // Counts taken from the loaded window rather than the board's index: say so, so a
      // small number is not read as "this topic has three messages".
      partial
        ? jsx('span', {
            className: 'shrink-0 pl-1 text-[0.625rem] text-(--ui-text-quaternary)',
            title: 'the board topic index was unavailable; these counts cover the loaded messages only',
            children: 'loaded'
          })
        : null
    ]
  })
}

function InboxStrip({ inbox }) {
  const asks = (inbox && inbox.asks) || []
  if (!asks.length) return null
  const stale = asks.filter(a => a.ageing === 'stale').length
  return jsxs('div', {
    className:
      'flex shrink-0 flex-col gap-1 border-b border-(--ui-stroke-secondary) bg-(--ui-fill-secondary) px-2 py-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-[0.6875rem]',
        children: [
          jsx('span', {
            className: cn('font-medium', stale ? 'text-(--ui-warning)' : 'text-(--ui-text-secondary)'),
            children: `${asks.length} waiting on you`
          }),
          jsx('span', {
            className: 'text-(--ui-text-quaternary)',
            children: asks
              .slice(0, 2)
              .map(a => `${VOICES[a.from] || a.from} ${age(a.age_days)}`)
              .join(' · ')
          })
        ]
      }),
      asks.slice(0, 3).map(ask =>
        jsxs(
          'div',
          {
            className: 'min-w-0 truncate text-[0.6875rem] text-(--ui-text-tertiary)',
            title: ask.text,
            children: [
              jsx('span', {
                className: cn(
                  'mr-1',
                  ask.ageing === 'stale' ? 'text-(--ui-warning)' : 'text-(--ui-text-quaternary)'
                ),
                children: age(ask.age_days)
              }),
              ask.text
            ]
          },
          ask.id
        )
      )
    ]
  })
}

function Composer({ channel, topic }) {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const send = () => {
    const body = text.trim()
    if (!body || !channel || busy) return
    setBusy(true)
    setError('')
    haptic('tap')
    const payload = { text: body, channel, via: 'human' }
    // Posting while a topic is selected tags the message with it: the filter is then
    // also the destination, rather than a view that silently applies to nothing.
    if (topic) payload.topic = topic
    pluginCtx
      .rest('/post', { method: 'POST', body: payload })
      .then(result => {
        if (!result || result.ok === false) {
          setError((result && result.error) || 'post failed')
          return
        }
        setText('') // only on success: a failure keeps the words
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
        placeholder: channel
          ? `type as yourself into ${channelLabel(channel)}${topic ? ` · #${topic}` : ''}`
          : 'no channel',
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
            children: error || 'posts as you · ⌘↵ to send'
          }),
          jsx(Button, {
            variant: 'primary',
            disabled: busy || !text.trim() || !channel,
            onClick: send,
            children: busy ? 'sending…' : 'send'
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
      const topic = activeTopic()
      const params = ['limit=200']
      if (channel) params.push(`channel=${encodeURIComponent(channel)}`)
      if (topic) params.push(`topic=${encodeURIComponent(topic)}`)
      return pluginCtx.rest('/state' + (params.length ? `?${params.join('&')}` : ''))
    },
    refetchInterval: POLL_MS
  })
}

function BoardBody({ compact }) {
  const query = useBoardState()
  const data = query.data
  const [expanded, setExpanded] = useState({})
  // Search and file preview are local to this view: the board answers them, the pane
  // does not remember them, and leaving the channel clears nothing the board owns.
  const [searchText, setSearchText] = useState('')
  const [results, setResults] = useState(null)
  const [preview, setPreview] = useState(null)

  if (query.isPending) {
    return jsx('div', {
      className: 'flex flex-col gap-2 p-3',
      children: [0, 1, 2].map(i => jsx(Skeleton, { className: 'h-6 w-full' }, i))
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
    return jsx(EmptyState, {
      title: 'No channels',
      description: 'the board has no channel you may read'
    })
  }

  const seen = seenMap()
  const messages = data.messages || []
  // The picked topic filters locally. The backend also accepts a topic, but a pane that
  // only responds when a long-lived process happens to have the newer module is a pane
  // that appears broken; filtering what is already loaded always works, and 200 messages
  // covers a channel this size whole.
  const pickedTopic = activeTopic()
  const visible = pickedTopic ? messages.filter(msg => msg.topic === pickedTopic) : messages
  const lastSeen = active ? seen[active] || 0 : 0
  const firstNew = visible.find(msg => msg.id > lastSeen)

  // The board's topic index is the accurate count. When the half serving /state predates
  // it, fall back to what is loaded rather than showing nothing: a partial index beats a
  // pane that looks broken, as long as it says it is partial.
  const indexAvailable = Array.isArray(data.topics)
  const topics = indexAvailable
    ? data.topics
    : (() => {
        const counts = new Map()
        for (const msg of messages) {
          if (!msg.topic) continue
          counts.set(msg.topic, (counts.get(msg.topic) || 0) + 1)
        }
        return [...counts.entries()]
          .sort((a, b) => b[1] - a[1])
          .map(([topic, count]) => ({ topic, messages: count }))
      })()


  // Search runs on submit rather than on every keystroke: the index is the board's,
  // and a ranked query per character would be noise for a human reading three results.
  const runSearch = () => {
    const text = searchText.trim()
    if (!text) {
      setResults(null)
      return
    }
    haptic('tap')
    pluginCtx
      .rest('/search?q=' + encodeURIComponent(text) + (active ? `&channel=${encodeURIComponent(active)}` : ''))
      .then(found => setResults(found && found.ok !== false ? found : { results: [] }))
      .catch(exc => setResults({ results: [], error: String(exc && exc.message ? exc.message : exc) }))
  }

  const openFile = file => {
    haptic('tap')
    setPreview({ loading: true, id: file.id, name: file.name })
    pluginCtx
      .rest('/file/' + file.id)
      .then(body => setPreview(body || { error: 'no answer' }))
      .catch(exc => setPreview({ error: String(exc && exc.message ? exc.message : exc) }))
  }

  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsx('div', {
        className: cn(
          'flex shrink-0 flex-col gap-0.5 overflow-y-auto p-1',
          compact ? 'max-h-28' : 'max-h-40'
        ),
        children: channels.map(channel =>
          jsx(
            ChannelRow,
            {
              channel,
              active: channel.channel === active,
              unread: (channel.last_id || 0) > (seen[channel.channel] || 0),
              delivery: channelDelivery(data.delivery, channel.channel),
              onSelect: () => {
                haptic('tap')
                setSelectedChannel(channel.channel)
                setActiveTopic(null)     // topics belong to a channel; do not carry one over
                markSeen([channel])
                void queryClient.invalidateQueries({ queryKey: STATE_KEY })
              }
            },
            channel.channel
          )
        )
      }),
      jsx(InboxStrip, { inbox: data.inbox }),
      jsx(TopicStrip, {
        topics,
        active: pickedTopic,
        partial: !indexAvailable,
        onPick: key => {
          haptic('tap')
          setActiveTopic(key)
          void queryClient.invalidateQueries({ queryKey: STATE_KEY })
        }
      }),
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-1 border-t border-(--ui-stroke-secondary) px-2 py-1',
        children: [
          jsx('input', {
            value: searchText,
            placeholder: 'search this channel',
            onChange: event => setSearchText(event.target.value),
            onKeyDown: event => {
              if (event.key === 'Enter') runSearch()
              if (event.key === 'Escape') {
                setSearchText('')
                setResults(null)
              }
            },
            className:
              'min-w-0 flex-1 rounded border border-(--ui-stroke-secondary) bg-transparent px-2 py-0.5 text-[0.6875rem] outline-none focus:border-(--ui-accent)'
          }),
          jsx('button', {
            type: 'button',
            className: 'shrink-0 text-[0.625rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
            onClick: results === null ? runSearch : () => {
              setSearchText('')
              setResults(null)
            },
            children: results === null ? 'find' : 'clear'
          })
        ]
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1 border-t border-(--ui-stroke-secondary)',
        children: jsx('div', {
          className: 'flex flex-col px-2',
          children:
            results !== null
              ? results.results && results.results.length
                ? results.results.map(hit =>
                    jsx(
                      Message,
                      {
                        msg: { ...hit, text: hit.snippet || hit.text },
                        expanded: true,
                        onToggle: () => {},
                        onFile: openFile,
                        showNewMarker: false
                      },
                      `hit-${hit.id}`
                    )
                  )
                : jsx(EmptyState, {
                    title: 'Nothing found',
                    description: results.error || `no match for “${searchText}” in ${active}`
                  })
              : visible.length
                ? visible.map(msg =>
                    jsx(
                      Message,
                      {
                        msg,
                        expanded: Boolean(expanded[msg.id]),
                        showNewMarker: firstNew !== undefined && msg.id === firstNew.id,
                        onFile: openFile,
                        onToggle: () => {
                          haptic('tap')
                          setExpanded(prev => ({ ...prev, [msg.id]: !prev[msg.id] }))
                        }
                      },
                      msg.id
                    )
                  )
                : jsx(EmptyState, {
                    title: pickedTopic ? `Nothing tagged #${pickedTopic}` : 'Nothing here yet',
                    description: pickedTopic
                      ? `this channel has no messages tagged #${pickedTopic} in the loaded window`
                      : channels.find(c => c.channel === active && c.state === 'unused') !== undefined
                        ? 'this room has never been used'
                        : 'no messages in this channel'
                  })
        })
      }),
      preview
        ? jsxs('div', {
            className:
              'flex max-h-56 shrink-0 flex-col gap-1 overflow-y-auto border-t border-(--ui-stroke-secondary) bg-(--ui-fill-secondary) px-2 py-1.5 text-[0.6875rem]',
            children: [
              jsxs('div', {
                className: 'flex items-center gap-2',
                children: [
                  jsx('span', { className: 'min-w-0 flex-1 truncate font-medium', children: preview.name || `#${preview.id}` }),
                  preview.size ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: `${preview.size} B` }) : null,
                  jsx('button', {
                    type: 'button',
                    className: 'shrink-0 text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
                    onClick: () => setPreview(null),
                    children: 'close'
                  })
                ]
              }),
              preview.loading
                ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'loading…' })
                : preview.error
                  ? jsx('span', { className: 'text-(--ui-danger)', children: preview.error })
                  : preview.text
                    ? jsx('pre', {
                        className: 'whitespace-pre-wrap text-[0.6875rem] leading-snug text-(--ui-text-secondary)',
                        style: { userSelect: 'text' },
                        children: preview.text
                      })
                    : jsx('span', { className: 'text-(--ui-text-quaternary)', children: preview.note || 'no preview' })
            ]
          })
        : null,
      jsx(Composer, { channel: active, topic: data.topic || null })
    ]
  })
}

function BoardPane() {
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className:
          'flex shrink-0 items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2',
        children: [
          jsx('span', { className: 'text-sm font-medium', children: 'Board' }),
          jsx('button', {
            type: 'button',
            className:
              'ml-auto text-[0.6875rem] text-(--ui-text-quaternary) hover:text-(--ui-text-secondary)',
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
        className:
          'flex items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-4 py-2',
        children: [
          jsx('div', { className: 'text-sm font-medium', children: 'Agent board' }),
          jsxs('div', {
            className: 'flex items-center gap-3 text-[0.6875rem] text-(--ui-text-quaternary)',
            children: [
              jsx('span', { children: 'oldest at the top · long posts collapse' }),
              jsx(Button, {
                variant: 'ghost',
                onClick: () => {
                  haptic('tap')
                  void queryClient.invalidateQueries({ queryKey: STATE_KEY })
                },
                children: 'Refresh'
              })
            ]
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
  const inbox = (data && data.inbox) || { asks: [] }
  const waiting = (inbox.asks || []).length
  const seen = seenMap()
  const unread = channels.filter(c => (c.last_id || 0) > (seen[c.channel] || 0)).length
  const down = query.isError || (data && data.ok === false)

  return jsx('button', {
    type: 'button',
    className: cn(
      'px-1.5 text-[0.6875rem] tabular-nums',
      down ? 'text-(--ui-danger)' : waiting ? 'text-(--ui-warning)' : 'text-(--ui-text-tertiary)',
      'hover:text-(--ui-text-primary)'
    ),
    title: down
      ? 'board unreachable'
      : waiting
        ? `${waiting} waiting on you in ${channels.length} channel(s)`
        : `${channels.length} channel(s), ${unread} with something new`,
    onClick: () => {
      haptic('tap')
      host.navigate('/board')
    },
    children: down ? 'board ·' : waiting ? `board ${waiting}!` : unread ? `board ${unread}` : 'board'
  })
}

// ------------------------------------------------------------------- register

export default {
  id: ID,
  name: 'Agent board',
  register(ctx) {
    pluginCtx = ctx

    // Seeing a channel counts as reading it, so the chip reports what is new since the
    // last look rather than everything that ever arrived.
    const markVisible = () => {
      const data = queryClient.getQueryData(STATE_KEY)
      if (data && data.channels) markSeen(data.channels)
    }

    // A contribution id is the key the app persists a dismissed pane under. This one is
    // new on purpose: the first install left 'pane' recorded as closed, and a plugin
    // whose only pane reads as closed is disabled the moment it is enabled.
    ctx.register({
      id: 'board-pane',
      area: 'panes',
      title: 'board',
      // Docked to the right edge of the conversation, not stacked as a tab in the
      // right-hand group: a pane that lands as a tab behind Review/Files is present
      // but invisible, which reads as "the plugin isn't working".
      data: { placement: 'right', dock: { pane: 'workspace', pos: 'right' }, width: '340px' },
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
        keywords: ['board', 'agent', 'channel', 'will', 'zeta', 'messages', 'inbox'],
        run: () => {
          haptic('tap')
          host.navigate('/board')
        }
      }
    })
  }
}
