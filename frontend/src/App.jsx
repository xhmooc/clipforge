import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core'
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'

import { api } from './api.js'

// 供货直播切片常用到的 Windows 中文字体
const FONTS = [
  { value: 'Microsoft YaHei', label: '微软雅黑' },
  { value: 'SimHei', label: '黑体' },
  { value: 'SimSun', label: '宋体' },
  { value: 'KaiTi', label: '楷体' },
  { value: 'DengXian', label: '等线' },
  { value: 'FangSong', label: '仿宋' },
]

// 成片画布是 1080×1920，而 libass 认的 PlayResY 是 384，
// 所以 ASS 字号要先乘 (1920/384) 才是画布单位。按这个系数换算，
// 预览里的字幕占屏比例才和成片一致——系数是和成片实测比对出来的。
const EXPORT_WIDTH = 1080
const FONT_TO_CANVAS = 1920 / 384

// 这几种状态意味着后端还有后台任务在跑，前端要轮询等它结束
const RUNNING_STATUSES = ['preparing', 'transcribing', 'analyzing', 'rendering', 'previewing']

const ZOOM_MIN = 1
const ZOOM_MAX = 100

// 吸附半径按「屏幕像素」算而不是按秒：放大看单句和缩着看全片的手感才一致
const SNAP_PIXELS = 8

// 贴纸在时间轴上有几条泳道，可以上下拖到别的道；每条道多高（px）
const STICKER_LANES = 3
const LANE_HEIGHT = 20
// 贴纸区在轨道区里的起始 y：刻度尺 17 + 字幕轨 22
const STICKER_TOP = 39

const fmt = (seconds) => {
  if (!Number.isFinite(seconds) || seconds < 0) return '00:00.0'
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes).padStart(2, '0')}:${(seconds % 60).toFixed(1).padStart(4, '0')}`
}

const fmtTick = (seconds) => {
  const minutes = Math.floor(seconds / 60)
  return `${String(minutes).padStart(2, '0')}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`
}

const uid = () => Math.random().toString(36).slice(2, 10)

/** 刻度间距：让整屏大约落在 8~14 根刻度上，太长太短都不好看。 */
function tickStepFor(span) {
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]
  const target = span / 10 || 1
  return steps.find((step) => step >= target) || steps[steps.length - 1]
}

/** 时间输入框：输入时不打断用户，失焦才提交，避免边打字边被格式化。 */
function TimeInput({ value, onCommit }) {
  const [draft, setDraft] = useState(String(value))
  useEffect(() => setDraft(String(value)), [value])
  return (
    <input
      type="number"
      step="0.1"
      value={draft}
      onClick={(event) => event.stopPropagation()}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        const parsed = Number(draft)
        if (Number.isFinite(parsed)) onCommit(parsed)
        else setDraft(String(value))
      }}
    />
  )
}

/** 可拖动排序的片段行。顺序就是导出拼接时的顺序。 */
function SortableClip({ clip, index, count, selected, onSelect, onPatch, onRemove, onSeek, onMove }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: clip.id,
  })
  return (
    <div
      ref={setNodeRef}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.5 : 1,
      }}
      className={`row clip${clip.mirror ? ' mirrored' : ''}${selected ? ' selected' : ''}`}
      onClick={onSelect}
    >
      <span className="handle" title="拖动调整顺序" {...attributes} {...listeners}>
        ⠿
      </span>
      <span className="order">{index + 1}</span>
      <span className="moves">
        <button className="ghost move" onClick={() => onMove(clip.id, -1)} disabled={index === 0}>
          ↑
        </button>
        <button
          className="ghost move"
          onClick={() => onMove(clip.id, 1)}
          disabled={index === count - 1}
        >
          ↓
        </button>
      </span>
      <input
        className="title"
        value={clip.title}
        onChange={(event) => onPatch(clip.id, { title: event.target.value })}
      />
      <label className="mirror" title="水平镜像这段画面（同一件货换个观感）">
        <input
          type="checkbox"
          checked={clip.mirror}
          onChange={(event) => onPatch(clip.id, { mirror: event.target.checked })}
        />
        镜像
      </label>
      <span className="range" onClick={() => onSeek(clip.start)}>
        {fmt(clip.start)} → {fmt(clip.end)}
      </span>
      <span className="dur">{(clip.end - clip.start).toFixed(1)}s</span>
      <button className="ghost" onClick={() => onRemove(clip.id)}>
        删除
      </button>
    </div>
  )
}

/** 一张贴纸：缩略图 + 位置/大小三个滑块。 */
function StickerRow({ sticker, url, onPatch, onRemove }) {
  return (
    <div className="sticker-row">
      <img className="thumb" src={url} alt={sticker.file} />
      <div className="sticker-controls">
        <span className="sticker-window" title="在贴纸轨上拖块的两边可以改这个时间段">
          出现 {fmt(sticker.start)} → {sticker.end > 0 ? fmt(sticker.end) : '片尾'}
        </span>
        <label>
          横向
          <input
            type="range"
            min="0"
            max="1"
            step="0.01"
            value={sticker.x}
            onChange={(event) => onPatch({ x: Number(event.target.value) })}
          />
        </label>
        <label>
          纵向
          <input
            type="range"
            min="0"
            max="1"
            step="0.01"
            value={sticker.y}
            onChange={(event) => onPatch({ y: Number(event.target.value) })}
          />
        </label>
        <label>
          大小
          <input
            type="range"
            min="0.05"
            max="1"
            step="0.01"
            value={sticker.scale}
            onChange={(event) => onPatch({ scale: Number(event.target.value) })}
          />
        </label>
      </div>
      <button className="ghost" onClick={onRemove}>
        删除
      </button>
    </div>
  )
}

export default function App() {
  const videoRef = useRef(null)
  const fileRef = useRef(null)
  const fontFileRef = useRef(null)
  const stickerFileRef = useRef(null)
  const rowsRef = useRef(null)
  const lastActive = useRef(-1)
  // 时间轴的「轨道区」：刻度尺 + 三条轨道都挂在它下面
  const lanesRef = useRef(null)
  // 9:16 的预览框，字幕和贴纸的拖动坐标都按它来算
  const frameRef = useRef(null)
  const stickerSaveTimer = useRef(null)
  // 拖动排序要有一个最小位移才触发，否则点选会被误判成拖拽
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))

  const [projects, setProjects] = useState([])
  const [project, setProject] = useState(null)
  const [subtitles, setSubtitles] = useState([])
  const [clips, setClips] = useState([])
  const [stickers, setStickers] = useState([])
  const [dirtySubtitles, setDirtySubtitles] = useState(false)
  const [dirtyClips, setDirtyClips] = useState(false)
  const [playhead, setPlayhead] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [mark, setMark] = useState({ in: null, out: null })
  const [style, setStyle] = useState({
    font: 'Microsoft YaHei',
    size: 18,
    color: '#FFFFFF',
    x: 0.5,
    y: 0.7917,
    // 字幕框宽度（占画面比例）。它和字号是两回事：字号管字多大，框宽管一行放几个字
    width: 0.86,
  })
  const [dirtyStyle, setDirtyStyle] = useState(false)
  const [selected, setSelected] = useState(-1)
  const [selectedClip, setSelectedClip] = useState('')
  // 「AI 找高光」要几段，0 表示不限
  const [highlightLimit, setHighlightLimit] = useState(3)
  // 右侧三个面板的高度，可以用面板之间的拖动条调；记在浏览器里，下次打开还是这个比例
  const [panelSizes, setPanelSizes] = useState(() => {
    try {
      // 带 v2 是为了让「字幕给足」这套新默认值生效一次，不然老记录会一直压着
      const raw = localStorage.getItem('clipwork.panels.v2')
      if (raw) return JSON.parse(raw)
    } catch {
      /* 读不出来就用默认值 */
    }
    // 默认把「字幕」给足——字幕列表是最常翻的，装饰就是个素材库
    return { subtitles: 420, decor: 84, clips: 300 }
  })
  // 右侧栏宽度，同样可以拖
  const [sideWidth, setSideWidth] = useState(() => {
    const saved = Number(localStorage.getItem('clipwork.sideWidth'))
    return Number.isFinite(saved) && saved >= 320 ? saved : 480
  })
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [showSubtitles, setShowSubtitles] = useState(true)
  const [videoWidth, setVideoWidth] = useState(0)
  const [dragging, setDragging] = useState(false)
  const [timelineDrag, setTimelineDrag] = useState(null)
  const [zoom, setZoom] = useState(1)
  const [viewStart, setViewStart] = useState(0)
  const [concatSrc, setConcatSrc] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [settings, setSettings] = useState(null)

  const refreshProjects = useCallback(async () => {
    setProjects(await api.listProjects())
  }, [])

  useEffect(() => {
    refreshProjects().catch((error) => setMessage(error.message))
  }, [refreshProjects])

  useEffect(() => {
    try {
      localStorage.setItem('clipwork.panels.v2', JSON.stringify(panelSizes))
      localStorage.setItem('clipwork.sideWidth', String(sideWidth))
    } catch {
      /* 存不了也不影响使用 */
    }
  }, [panelSizes, sideWidth])

  // 「要几段高光」是存在设置里的，开机读一次，省得每次都被打回默认的 3
  useEffect(() => {
    api
      .getSettings()
      .then((data) => {
        const limit = data?.highlight?.limit
        if (Number.isFinite(limit)) setHighlightLimit(limit)
      })
      .catch(() => {})
  }, [])

  // 转写和渲染都是后端的后台任务，这里轮询到状态不再是进行中为止
  useEffect(() => {
    if (!RUNNING_STATUSES.includes(project?.status)) return undefined
    const phase = project.status

    const timer = setInterval(async () => {
      try {
        const fresh = await api.getProject(project.id)
        setProject(fresh)
        if (RUNNING_STATUSES.includes(fresh.status)) return
        if (phase === 'preparing') {
          setMessage(fresh.status === 'ready' ? '预览转码完成，可以转写了' : fresh.message)
        } else if (phase === 'transcribing') {
          setSubtitles(fresh.subtitles)
          setDirtySubtitles(false)
          setMessage(`转写完成，共 ${fresh.subtitles.length} 条字幕`)
        } else if (phase === 'analyzing') {
          setMessage(fresh.message || `AI 找到 ${fresh.segments.length} 段商品讲解`)
        } else if (phase === 'previewing') {
          setMessage(fresh.status === 'ready' ? fresh.message : fresh.message)
          if (fresh.concat_preview) setConcatSrc(`${api.concatUrl(fresh.id)}?s=${Date.now()}`)
        } else {
          setMessage(
            fresh.status === 'done'
              ? `导出完成，共 ${fresh.renders.length} 个切片`
              : fresh.message,
          )
        }
      } catch (error) {
        setMessage(error.message)
      }
    }, 3000)
    return () => clearInterval(timer)
  }, [project?.id, project?.status])

  const open = useCallback(async (id) => {
    if (!id) {
      setProject(null)
      setSubtitles([])
      setClips([])
      setStickers([])
      return
    }
    try {
      const loaded = await api.getProject(id)
      setProject(loaded)
      setSubtitles(loaded.subtitles)
      setClips(loaded.clips)
      setStickers(loaded.stickers)
      setDirtySubtitles(false)
      setDirtyClips(false)
      setDirtyStyle(false)
      setStyle({
        font: loaded.subtitle_font,
        size: loaded.subtitle_size,
        color: loaded.subtitle_color,
        x: loaded.subtitle_x ?? 0.5,
        y: loaded.subtitle_y ?? 0.7917,
        width: loaded.subtitle_width ?? 0.86,
      })
      setSelected(-1)
      setSelectedClip('')
      setMark({ in: null, out: null })
      setZoom(1)
      setViewStart(0)
      setConcatSrc('')
      setMessage('')
    } catch (error) {
      setMessage(error.message)
    }
  }, [])

  const activeIndex = useMemo(() => {
    if (!subtitles.length) return -1
    return subtitles.findIndex((cue) => playhead >= cue.start && playhead < cue.end)
  }, [subtitles, playhead])

  const activeCue = activeIndex >= 0 ? subtitles[activeIndex] : null
  const overlayFontPx = videoWidth
    ? (videoWidth * style.size * FONT_TO_CANVAS) / EXPORT_WIDTH
    : 0

  // 播放到哪，就把对应的字幕行滚进视野
  useEffect(() => {
    if (activeIndex < 0 || activeIndex === lastActive.current) return
    lastActive.current = activeIndex
    rowsRef.current
      ?.querySelector(`[data-index="${activeIndex}"]`)
      ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [activeIndex])

  // 预览字幕要按视频实际显示宽度换算字号，所以要盯着 video 的尺寸变化
  useEffect(() => {
    const video = videoRef.current
    if (!video) return undefined
    const observer = new ResizeObserver((entries) => {
      const width = entries[0]?.contentRect.width
      if (width) setVideoWidth(width)
    })
    observer.observe(video)
    return () => observer.disconnect()
  }, [project?.id, concatSrc])

  const seek = (seconds) => {
    const video = videoRef.current
    if (!video) return
    video.currentTime = seconds
    setPlayhead(seconds)
  }

  const totalDuration = project?.duration || 0
  // 一屏能看到多长的时间 = 总长 / 缩放倍数
  const timeWindow = zoom > 0 ? totalDuration / zoom : totalDuration

  const clampView = useCallback(
    (value) => Math.max(0, Math.min(value, Math.max(0, totalDuration - timeWindow))),
    [totalDuration, timeWindow],
  )

  const positionToTime = (clientX) => {
    const rect = lanesRef.current?.getBoundingClientRect()
    if (!rect || !rect.width || !totalDuration) return 0
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width))
    return viewStart + ratio * timeWindow
  }

  const toPercent = (seconds) => ((seconds - viewStart) / (timeWindow || 1)) * 100
  const inView = (seconds) =>
    totalDuration > 0 && seconds >= viewStart && seconds <= viewStart + timeWindow

  // 吸附的候选点：其他块的两条边、入出点、播放头、片头片尾。
  // owner 用来把「正在拖的那个元素自己」排除掉，否则它会吸在自己身上。
  const snapTargets = useMemo(() => {
    const list = [
      { time: 0, kind: 'bounds' },
      { time: totalDuration, kind: 'bounds' },
    ]
    clips.forEach((clip) => {
      list.push({ time: clip.start, kind: 'clip', owner: `clip:${clip.id}` })
      list.push({ time: clip.end, kind: 'clip', owner: `clip:${clip.id}` })
    })
    stickers.forEach((item) => {
      list.push({ time: item.start, kind: 'sticker', owner: `sticker:${item.id}` })
      list.push({
        time: item.end > 0 ? item.end : totalDuration,
        kind: 'sticker',
        owner: `sticker:${item.id}`,
      })
    })
    subtitles.forEach((cue, index) => {
      list.push({ time: cue.start, kind: 'subtitle', owner: `cue:${index}` })
      list.push({ time: cue.end, kind: 'subtitle', owner: `cue:${index}` })
    })
    if (mark.in != null) list.push({ time: mark.in, kind: 'marks' })
    if (mark.out != null) list.push({ time: mark.out, kind: 'marks' })
    list.push({ time: playhead, kind: 'playhead' })
    return list
  }, [clips, stickers, subtitles, mark.in, mark.out, playhead, totalDuration])

  /**
   * 吸不到就返回 null，调用方据此决定用不用它，免得把「没吸」和「吸到了同一位置」搞混。
   *
   * kinds 决定允许吸哪一类参照：拖片段时不去吸字幕断句——一场 4 小时的直播有上千条字幕，
   * 句边界密到几乎处处都在吸附半径内，那样手感会一直「发黏」，反而拖不准。
   */
  const snapTime = useCallback(
    (time, owner, kinds) => {
      const width = lanesRef.current?.getBoundingClientRect().width || 0
      if (!width || !totalDuration) return null
      const tolerance = (SNAP_PIXELS / width) * timeWindow
      let best = null
      let bestDistance = tolerance
      for (const target of snapTargets) {
        if (owner && target.owner === owner) continue
        if (kinds && !kinds.includes(target.kind)) continue
        const distance = Math.abs(target.time - time)
        if (distance <= bestDistance) {
          best = target.time
          bestDistance = distance
        }
      }
      return best
    },
    [snapTargets, timeWindow, totalDuration],
  )

  /** 前后切换：把播放头挪到上一段 / 下一段的起点。 */
  const stepClip = (direction) => {
    if (!clips.length) return
    const ordered = [...clips].sort((a, b) => a.start - b.start)
    const epsilon = 0.05
    const target =
      direction > 0
        ? ordered.find((clip) => clip.start > playhead + epsilon)
        : [...ordered].reverse().find((clip) => clip.start < playhead - epsilon)
    if (!target) {
      setMessage(direction > 0 ? '已经是最后一段了' : '已经是第一段了')
      return
    }
    setSelectedClip(target.id)
    seek(target.start)
  }

  /**
   * 拖两个面板之间的横条：上面那块变高多少，下面就矮多少，加起来不变，
   * 所以整个侧栏的高度不会动，也就不会冒出滚动条。
   */
  const startSplitDrag = (event, topKey, bottomKey) => {
    event.preventDefault()
    const startY = event.clientY
    const startTop = panelSizes[topKey]
    const total = startTop + panelSizes[bottomKey]
    const floor = 64

    const onMove = (moveEvent) => {
      const wanted = startTop + (moveEvent.clientY - startY)
      const top = Math.max(floor, Math.min(wanted, total - floor))
      setPanelSizes((sizes) => ({ ...sizes, [topKey]: top, [bottomKey]: total - top }))
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  /** 拖舞台和右侧栏之间那条竖线：往右拖，右侧栏变窄。 */
  const startSideDrag = (event) => {
    event.preventDefault()
    const startX = event.clientX
    const startWidth = sideWidth

    const onMove = (moveEvent) => {
      // 上限是给画面留出至少 320px，而不是按窗口比例——窗口窄的时候按比例算
      // 会把上限压得比当前宽度还小，一拖反而把侧栏挤扁
      const limit = Math.max(320, window.innerWidth - 320)
      setSideWidth(Math.max(280, Math.min(startWidth - (moveEvent.clientX - startX), limit)))
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  /** 按鼠标纵坐标算出贴纸该落在第几条泳道。 */
  const laneFromPointer = (clientY) => {
    const rect = lanesRef.current?.getBoundingClientRect()
    if (!rect) return null
    const offset = clientY - rect.top - STICKER_TOP
    return Math.max(0, Math.min(STICKER_LANES - 1, Math.floor(offset / LANE_HEIGHT)))
  }

  /**
   * 磁吸挤压：每次都从「按下鼠标那一刻的快照」重算全部片段的位置。
   *
   * 被拖的那段落到哪，原来在它右边、又被盖住的邻居就整段往右让开（拖着一起走），
   * 左边同理往左让。用快照重算而不是在上一次结果上叠加，是为了往回拖时邻居能
   * 回到原位——否则它们会被越推越远。
   */
  const layoutClips = (snapshot, id, start, end) => {
    const moved = snapshot.find((item) => item.id === id)
    if (!moved) return snapshot
    const length = Math.max(0.1, end - start)
    const left = Math.max(0, start)
    const right = left + length
    const others = snapshot
      .filter((item) => item.id !== id)
      .sort((a, b) => a.start - b.start)
    const shifted = new Map()

    let cursor = right
    for (const clip of others) {
      if (clip.start < moved.start) continue
      if (clip.start >= cursor) {
        cursor = clip.end
        continue
      }
      const delta = cursor - clip.start
      shifted.set(clip.id, { start: clip.start + delta, end: clip.end + delta })
      cursor = clip.end + delta
    }

    let back = left
    for (const clip of [...others].reverse()) {
      if (clip.start >= moved.start) continue
      if (clip.end <= back) {
        back = clip.start
        continue
      }
      const delta = back - clip.end
      if (clip.start + delta < 0) continue
      shifted.set(clip.id, { start: clip.start + delta, end: clip.end + delta })
      back = clip.start + delta
    }

    return snapshot.map((clip) => {
      if (clip.id === id) return { ...clip, start: left, end: right }
      const next = shifted.get(clip.id)
      return next ? { ...clip, ...next } : clip
    })
  }

  // 播放头跑出视野就把它拉回来；只在真的在放的时候跟，免得手动平移被拽走
  useEffect(() => {
    if (!playing || zoom <= 1 || !totalDuration) return
    if (playhead < viewStart || playhead > viewStart + timeWindow) {
      setViewStart(clampView(playhead - timeWindow * 0.25))
    }
  }, [playhead, playing, zoom, totalDuration, timeWindow, viewStart, clampView])

  const zoomBy = (factor, anchorRatio = 0.5) => {
    if (!totalDuration) return
    const anchor = viewStart + anchorRatio * timeWindow
    const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom * factor))
    const nextWindow = totalDuration / next
    setZoom(next)
    setViewStart(
      Math.max(0, Math.min(anchor - anchorRatio * nextWindow, Math.max(0, totalDuration - nextWindow))),
    )
  }

  // 滚轮缩放是以鼠标位置为锚点的，所以要用原生监听才能 preventDefault
  useEffect(() => {
    const element = lanesRef.current
    if (!element || !totalDuration) return undefined
    const onWheel = (event) => {
      event.preventDefault()
      const rect = element.getBoundingClientRect()
      if (!rect.width) return
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width))
      zoomBy(event.deltaY < 0 ? 1.4 : 1 / 1.4, ratio)
    }
    element.addEventListener('wheel', onWheel, { passive: false })
    return () => element.removeEventListener('wheel', onWheel)
  }, [totalDuration, zoom, viewStart, timeWindow])

  // 时间轴上拖字幕块 / 片段：边缘改起止时间，中间整块平移，两种都带吸附
  useEffect(() => {
    if (!timelineDrag) return undefined
    const { target, kind, owner } = timelineDrag

    const write = (start, end, lane) => {
      if (target === 'sticker') {
        const patch = { start: Math.max(0, start) }
        // 上下拖动时换泳道
        if (lane != null) patch.track = lane
        // 原来「一直显示到片尾」的贴纸（end=0），平移时别把 end 写死，否则就丢了这个语义
        if (timelineDrag.openEnd && kind === 'move') {
          patchSticker(timelineDrag.id, patch)
          return
        }
        patchSticker(timelineDrag.id, { ...patch, end: Math.max(0, end) })
        return
      }
      const patch = { start: Math.max(0, start), end: Math.max(0, end) }
      if (target === 'cue') patchCue(timelineDrag.index, patch)
      else patchClip(timelineDrag.id, patch)
    }

    // 拖字幕可以吸相邻断句；拖片段/贴纸只吸块级边界，否则会被上千条字幕黏住
    const kinds = ['clip', 'sticker', 'marks', 'playhead', 'bounds']

    const onMove = (event) => {
      const raw = positionToTime(event.clientX)

      if (kind === 'edge') {
        const snapped = snapTime(raw, owner, kinds)
        const value = Math.max(0, snapped == null ? raw : snapped)
        if (target === 'cue') patchCue(timelineDrag.index, { [timelineDrag.side]: value })
        else if (target === 'sticker') patchSticker(timelineDrag.id, { [timelineDrag.side]: value })
        else patchClip(timelineDrag.id, { [timelineDrag.side]: value })
        return
      }

      // 整块平移：左右两条边各自去试吸附，哪个吸得近就用哪个来定整块的位置
      const length = timelineDrag.end - timelineDrag.start
      const rawStart = Math.max(0, timelineDrag.start + (raw - timelineDrag.grab))
      const candidates = []
      const left = snapTime(rawStart, owner, kinds)
      if (left != null) candidates.push({ start: left, distance: Math.abs(left - rawStart) })
      const right = snapTime(rawStart + length, owner, kinds)
      if (right != null) {
        candidates.push({ start: right - length, distance: Math.abs(right - (rawStart + length)) })
      }
      candidates.sort((a, b) => a.distance - b.distance)
      const start = candidates.length ? candidates[0].start : rawStart

      if (target === 'clip') {
        // 片段是「吸附 + 挤压」：从按下鼠标时的快照重算，挡路的邻居会被顶开
        setClips(layoutClips(timelineDrag.snapshot, timelineDrag.id, start, start + length))
        setDirtyClips(true)
        return
      }

      write(start, start + length, target === 'sticker' ? laneFromPointer(event.clientY) : null)
    }

    const onUp = () => setTimelineDrag(null)
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    return () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
  }, [timelineDrag, subtitles, clips, totalDuration, timeWindow, viewStart, snapTime])

  const handleUpload = async (event) => {
    const file = event.target.files?.[0]
    if (fileRef.current) fileRef.current.value = ''
    await uploadFile(file)
  }

  const uploadFile = async (file) => {
    if (!file) return
    setBusy(true)
    setMessage('上传中…')
    try {
      const created = await api.upload(file)
      await refreshProjects()
      await open(created.id)
      setMessage('已导入，正在转码预览…')
    } catch (error) {
      setMessage(error.message)
    } finally {
      setBusy(false)
    }
  }

  const handleDrop = async (event) => {
    event.preventDefault()
    setDragging(false)
    await uploadFile(event.dataTransfer.files?.[0])
  }

  const openSettings = async () => {
    try {
      setSettings(await api.getSettings())
      setShowSettings(true)
    } catch (error) {
      setMessage(error.message)
    }
  }

  const patchSettings = (section, patch) => {
    setSettings((current) => ({ ...current, [section]: { ...current[section], ...patch } }))
  }

  const saveSettings = async () => {
    try {
      setSettings(await api.saveSettings(settings))
      setShowSettings(false)
      setMessage('设置已保存')
    } catch (error) {
      setMessage(error.message)
    }
  }

  const handleTranscribe = async () => {
    if (!project) return
    setBusy(true)
    try {
      setProject(await api.transcribe(project.id))
      setMessage('转写中…')
    } catch (error) {
      setMessage(error.message)
    } finally {
      setBusy(false)
    }
  }

  const patchCue = (index, patch) => {
    setSubtitles((list) => list.map((cue, i) => (i === index ? { ...cue, ...patch } : cue)))
    setDirtySubtitles(true)
  }

  const patchStyle = (patch) => {
    setStyle((current) => ({ ...current, ...patch }))
    setDirtyStyle(true)
  }

  const saveSubtitlePanel = async () => {
    try {
      if (dirtySubtitles) {
        const saved = await api.saveSubtitles(project.id, subtitles)
        setSubtitles(saved.subtitles)
        setDirtySubtitles(false)
      }
      if (dirtyStyle) {
        await api.saveStyle(project.id, {
          subtitle_font: style.font,
          subtitle_size: style.size,
          subtitle_color: style.color,
          subtitle_x: style.x,
          subtitle_y: style.y,
          subtitle_width: style.width,
        })
        setDirtyStyle(false)
      }
      setMessage('字幕与样式已保存')
    } catch (error) {
      setMessage(error.message)
    }
  }

  const addClip = () => {
    if (mark.in == null || mark.out == null || mark.out <= mark.in) {
      setMessage('先设好入点和出点，出点要晚于入点')
      return
    }
    setClips((list) => [
      ...list,
      { id: uid(), start: mark.in, end: mark.out, title: `片段 ${list.length + 1}`, mirror: false },
    ])
    setDirtyClips(true)
    setMark({ in: null, out: null })
    setMessage('片段已加入列表，记得保存')
  }

  const clearMark = () => setMark({ in: null, out: null })

  const patchClip = (id, patch) => {
    setClips((list) => list.map((clip) => (clip.id === id ? { ...clip, ...patch } : clip)))
    setDirtyClips(true)
  }

  const removeClip = (id) => {
    setClips((list) => list.filter((clip) => clip.id !== id))
    setDirtyClips(true)
  }

  /** 在播放头处把选中的片段剪成两段。 */
  const splitClip = () => {
    const clip = clips.find((item) => item.id === selectedClip)
    if (!clip) {
      setMessage('先在片段列表或时间轴上点一段，再剪')
      return
    }
    if (playhead <= clip.start + 0.2 || playhead >= clip.end - 0.2) {
      setMessage('把播放头移到这一段中间再剪')
      return
    }
    const head = { ...clip, end: playhead, title: `${clip.title}（上）` }
    const tail = { ...clip, id: uid(), start: playhead, title: `${clip.title}（下）` }
    setClips((list) => list.flatMap((item) => (item.id === clip.id ? [head, tail] : [item])))
    setSelectedClip(tail.id)
    setDirtyClips(true)
    setMessage(`已在 ${fmt(playhead)} 处剪成两段`)
  }

  /** 把选中的片段和它后面那一段合并。镜像属性以选中的这段为准。 */
  const mergeClip = () => {
    const clip = clips.find((item) => item.id === selectedClip)
    if (!clip) {
      setMessage('先点一段再合并')
      return
    }
    const ordered = [...clips].sort((a, b) => a.start - b.start)
    const index = ordered.findIndex((item) => item.id === clip.id)
    const next = ordered[index + 1]
    if (!next) {
      setMessage('这已经是最后一段了')
      return
    }
    const merged = {
      ...clip,
      start: Math.min(clip.start, next.start),
      end: Math.max(clip.end, next.end),
    }
    setClips((list) =>
      list
        .filter((item) => item.id !== next.id)
        .map((item) => (item.id === clip.id ? merged : item)),
    )
    setDirtyClips(true)
    setMessage('已合并成一段，记得保存')
  }

  /**
   * 在预览画面上直接拖贴纸。
   * 位置按「抓取点占贴纸的比例」换算，所以拖起来不会把贴纸一下甩到鼠标位置。
   */
  const startStickerDrag = (event, item) => {
    event.preventDefault()
    event.stopPropagation()
    const frame = frameRef.current
    if (!frame) return
    const frameRect = frame.getBoundingClientRect()
    const box = event.currentTarget.getBoundingClientRect()
    const grabX = (event.clientX - box.left) / (box.width || 1)
    const grabY = (event.clientY - box.top) / (box.height || 1)
    const widthRatio = box.width / (frameRect.width || 1)
    const heightRatio = box.height / (frameRect.height || 1)

    const onMove = (moveEvent) => {
      const x = (moveEvent.clientX - frameRect.left) / frameRect.width - grabX * widthRatio
      const y = (moveEvent.clientY - frameRect.top) / frameRect.height - grabY * heightRatio
      patchSticker(item.id, { x: Math.min(1, Math.max(0, x)), y: Math.min(1, Math.max(0, y)) })
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  /**
   * 在预览画面上直接拖字幕。
   * ASS 里的锚点是文字块底边的中点，所以这里换算出来的 (x, y) 也用同一个锚点，
   * 预览和成片才会落在同一个位置。
   */
  const startSubtitleDrag = (event) => {
    event.preventDefault()
    event.stopPropagation()
    const frame = frameRef.current
    if (!frame) return
    const frameRect = frame.getBoundingClientRect()
    const box = event.currentTarget.getBoundingClientRect()
    const grabX = (event.clientX - box.left) / (box.width || 1)
    const grabY = (event.clientY - box.top) / (box.height || 1)

    const onMove = (moveEvent) => {
      const left =
        (moveEvent.clientX - frameRect.left) - grabX * box.width + box.width / 2
      const bottom = (moveEvent.clientY - frameRect.top) - grabY * box.height + box.height
      patchStyle({
        x: Math.min(1, Math.max(0, left / frameRect.width)),
        y: Math.min(1, Math.max(0, bottom / frameRect.height)),
      })
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  /**
   * 拖字幕框右下角那个小方块，改的是「字幕框的宽度」，不是字号。
   *
   * 框是水平居中锚在 (x, y) 上的，所以「鼠标到锚点横坐标的距离」就是半个框宽。
   * 字号完全不动，文字会在这个新宽度里重新折行——框拉宽，一行就能多放几个字。
   */
  const startSubtitleResize = (event) => {
    event.preventDefault()
    event.stopPropagation()
    const frame = frameRef.current
    if (!frame) return
    const rect = frame.getBoundingClientRect()
    const anchorX = style.x * rect.width

    const onMove = (moveEvent) => {
      const half = Math.abs(moveEvent.clientX - rect.left - anchorX)
      patchStyle({ width: Math.min(1, Math.max(0.15, (half * 2) / rect.width)) })
    }
    const onUp = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  const handleReveal = async () => {
    if (!project) return
    try {
      const result = await api.revealExport(project.id)
      setMessage(`已打开目录：${result.dir}`)
    } catch (error) {
      setMessage(error.message)
    }
  }

  const handleDragEnd = ({ active, over }) => {
    if (!over || active.id === over.id) return
    setClips((list) =>
      arrayMove(
        list,
        list.findIndex((clip) => clip.id === active.id),
        list.findIndex((clip) => clip.id === over.id),
      ),
    )
    setDirtyClips(true)
  }

  const moveClip = (id, delta) => {
    setClips((list) => {
      const from = list.findIndex((clip) => clip.id === id)
      const to = from + delta
      if (from < 0 || to < 0 || to >= list.length) return list
      return arrayMove(list, from, to)
    })
    setDirtyClips(true)
  }

  const handleAnalyze = async () => {
    if (!project) return
    setBusy(true)
    try {
      setProject(await api.analyze(project.id, highlightLimit))
      setMessage(`正在让模型找商品讲解段（最多 ${highlightLimit || '不限'} 段）…`)
    } catch (error) {
      setMessage(error.message)
    } finally {
      setBusy(false)
    }
  }

  /** 商品讲解段里已经挑好了一个精选窗口，直接拿它当片段最省事。 */
  const segmentToClip = (segment) => {
    const pick = segment.clips[0]
    return {
      id: uid(),
      start: pick ? pick.start : segment.start,
      end: pick ? pick.end : segment.end,
      title: pick?.title || segment.name,
      mirror: false,
    }
  }

  const addSegment = (segment) => {
    setClips((list) => [...list, segmentToClip(segment)])
    setDirtyClips(true)
    setMessage(`已加入「${segment.name}」，记得保存`)
  }

  const addAllSegments = () => {
    if (!project?.segments?.length) return
    setClips((list) => [...list, ...project.segments.map(segmentToClip)])
    setDirtyClips(true)
    setMessage(`已加入 ${project.segments.length} 段，记得保存`)
  }

  const saveClips = async () => {
    try {
      const saved = await api.saveClips(project.id, clips)
      setClips(saved.clips)
      setDirtyClips(false)
      setMessage(`已保存 ${saved.clips.length} 个片段`)
    } catch (error) {
      setMessage(error.message)
    }
  }

  const handleRender = async () => {
    if (!project) return
    if (!clips.length) {
      setMessage('先加至少一个片段')
      return
    }
    setBusy(true)
    try {
      // 后端是从磁盘读片段的，所以渲染前必须先把没保存的改动落盘
      if (dirtyClips) {
        await api.saveClips(project.id, clips)
        setDirtyClips(false)
      }
      setProject(await api.render(project.id))
      setMessage('正在导出切片…')
    } catch (error) {
      setMessage(error.message)
    } finally {
      setBusy(false)
    }
  }

  const handleConcatPreview = async () => {
    if (!project) return
    if (!clips.length) {
      setMessage('先加至少一个片段，再看拼接')
      return
    }
    setBusy(true)
    try {
      if (dirtyClips) {
        await api.saveClips(project.id, clips)
        setDirtyClips(false)
      }
      setProject(await api.buildPreview(project.id))
      setMessage('正在渲染并拼接各片段…')
    } catch (error) {
      setMessage(error.message)
    } finally {
      setBusy(false)
    }
  }

  const handleFontUpload = async (event) => {
    const file = event.target.files?.[0]
    if (fontFileRef.current) fontFileRef.current.value = ''
    if (!file || !project) return
    try {
      const updated = await api.uploadFont(project.id, file)
      setProject(updated)
      setStyle((current) => ({ ...current, font: updated.subtitle_font }))
      setDirtyStyle(false)
      setMessage(`字体「${updated.subtitle_font}」已上传，字幕样式已切过去`)
    } catch (error) {
      setMessage(error.message)
    }
  }

  const handleStickerUpload = async (event) => {
    const file = event.target.files?.[0]
    if (stickerFileRef.current) stickerFileRef.current.value = ''
    if (!file || !project) return
    try {
      const updated = await api.uploadSticker(project.id, file)
      setProject(updated)
      setStickers(updated.stickers)
      setMessage(`贴纸「${file.name}」已上传，拖滑块调位置`)
    } catch (error) {
      setMessage(error.message)
    }
  }

  // 滑块是连续触发的，攒一下再落盘，不然一次拖动就是几十个请求
  const patchSticker = (id, patch) => {
    setStickers((list) => {
      const next = list.map((item) => (item.id === id ? { ...item, ...patch } : item))
      if (stickerSaveTimer.current) clearTimeout(stickerSaveTimer.current)
      stickerSaveTimer.current = setTimeout(async () => {
        try {
          const saved = await api.saveStickers(project.id, next)
          setStickers(saved.stickers)
        } catch (error) {
          setMessage(error.message)
        }
      }, 350)
      return next
    })
  }

  const removeSticker = async (sticker) => {
    try {
      const updated = await api.deleteSticker(project.id, sticker.file)
      setProject(updated)
      setStickers(updated.stickers)
      setMessage('贴纸已删除')
    } catch (error) {
      setMessage(error.message)
    }
  }

  // I / O 标点，回车加片段——剪辑时手不用离开键盘
  useEffect(() => {
    const onKey = (event) => {
      const tag = event.target?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
      if (event.key === 'i' || event.key === 'I') setMark((m) => ({ ...m, in: playhead }))
      else if (event.key === 'o' || event.key === 'O') setMark((m) => ({ ...m, out: playhead }))
      else if (event.key === 'Enter') addClip()
      else if (event.key === 'ArrowLeft') {
        event.preventDefault()
        stepClip(-1)
      } else if (event.key === 'ArrowRight') {
        event.preventDefault()
        stepClip(1)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // 时间轴上的平移：移动超过阈值才算拖，没动就当成点击定位
  const startTimelinePan = (event) => {
    if (!totalDuration) return
    setSelected(-1)
    const startX = event.clientX
    const startView = viewStart
    const width = lanesRef.current?.getBoundingClientRect().width || 1
    let moved = false
    const onMove = (moveEvent) => {
      if (!moved && Math.abs(moveEvent.clientX - startX) < 4) return
      moved = true
      setViewStart(clampView(startView + ((startX - moveEvent.clientX) / width) * timeWindow))
    }
    const onUp = (upEvent) => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      if (!moved) seek(positionToTime(upEvent.clientX))
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  const fontOptions = useMemo(() => [
    ...FONTS,
    ...(project?.fonts || [])
      .filter((name) => !FONTS.some((item) => item.value === name))
      .map((name) => ({ value: name, label: `${name}（已上传）` })),
  ], [project?.fonts])

  const activeClip = clips.find((clip) => playhead >= clip.start && playhead < clip.end)
  const previewMirror = Boolean(activeClip?.mirror)
  const orientationLabel =
    project?.source_width > 0
      ? `${project.orientation === 'portrait' ? '竖屏源' : '横屏源'} ${project.source_width}×${project.source_height}`
      : ''

  const tickStep = totalDuration ? tickStepFor(timeWindow) : 60
  const ticks = []
  if (totalDuration) {
    for (
      let t = Math.ceil(viewStart / tickStep) * tickStep;
      t <= viewStart + timeWindow + 1e-6;
      t += tickStep
    ) {
      ticks.push(t)
    }
  }

  return (
    <div
      className="app"
      onDragOver={(event) => {
        event.preventDefault()
        setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
    >
      {dragging && <div className="drop-hint">松手即导入回放</div>}
      <header className="bar">
        <h1>ClipWork</h1>
        <select value={project?.id || ''} onChange={(event) => open(event.target.value)}>
          <option value="">选择工程…</option>
          {projects.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        <button onClick={() => fileRef.current?.click()} disabled={busy}>
          导入回放
        </button>
        <button
          className="primary"
          onClick={handleTranscribe}
          disabled={!project || busy || project.status === 'transcribing'}
        >
          转写
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="video/*,.flv,.ts,.mkv"
          hidden
          onChange={handleUpload}
        />
        <span className="orientation">{orientationLabel}</span>
        <span className={`status s-${project?.status || 'none'}`}>
          {project ? project.status : '未选择'}
          {project?.status === 'transcribing' ? ` ${project.progress}%` : ''}
        </span>
        <span className="message">{message}</span>
        <button onClick={openSettings}>设置</button>
      </header>

      <div className="body">
        <section className="stage">
          {project ? (
            <>
              <div className="stage-wrap">
                <div
                  ref={frameRef}
                  className={`frame ${project.orientation === 'portrait' ? 'portrait' : 'landscape'}`}
                >
                  <video
                    ref={videoRef}
                    key={concatSrc || 'source'}
                    src={concatSrc || api.videoUrl(project.id)}
                    controls
                    onTimeUpdate={(event) => setPlayhead(event.currentTarget.currentTime)}
                    onPlay={() => setPlaying(true)}
                    onPause={() => setPlaying(false)}
                    style={previewMirror ? { transform: 'scaleX(-1)' } : undefined}
                  />
                  {!concatSrc && showSubtitles && activeCue && overlayFontPx > 0 && (
                    <div
                      className="overlay"
                      title="可以直接拖动字幕"
                      onPointerDown={startSubtitleDrag}
                      style={{
                        left: `${style.x * 100}%`,
                        top: `${style.y * 100}%`,
                        width: `${style.width * 100}%`,
                        transform: 'translate(-50%, -100%)',
                        fontFamily: style.font,
                        fontSize: `${overlayFontPx}px`,
                        color: style.color,
                        WebkitTextStroke: `${Math.max(1, overlayFontPx * 0.06)}px #000`,
                      }}
                    >
                      {activeCue.text}
                      <span
                        className="overlay-handle"
                        title="左右拖这个角，改字幕框的宽度（字号不变）"
                        onPointerDown={startSubtitleResize}
                      />
                    </div>
                  )}
                  {!concatSrc &&
                    stickers
                      .filter(
                        (item) =>
                          item.start <= playhead && (item.end <= 0 || playhead <= item.end),
                      )
                      .map((item) => (
                        <img
                          key={item.id}
                          className="sticker-overlay"
                          src={api.stickerUrl(project.id, item.file)}
                          alt=""
                          title="可以直接拖动贴纸"
                          onPointerDown={(event) => startStickerDrag(event, item)}
                          style={{
                            width: `${item.scale * 100}%`,
                            left: `${item.x * 100}%`,
                            top: `${item.y * 100}%`,
                            transform: `translate(${-item.x * 100}%, ${-item.y * 100}%)`,
                          }}
                        />
                      ))}
                </div>
              </div>

              {concatSrc ? (
                <div className="toolbar">
                  <span className="badge">正在预览拼接结果（字幕、贴纸、镜像都已烧进画面）</span>
                  <button onClick={() => setConcatSrc('')}>返回原片继续编辑</button>
                </div>
              ) : (
                <>
                  <div className="toolbar">
                    <div className="tool-group">
                      <span className="playhead">当前 {fmt(playhead)}</span>
                    </div>
                    <div className="tool-group">
                      <button onClick={() => setMark((m) => ({ ...m, in: playhead }))}>
                        标入点 <kbd>I</kbd>
                      </button>
                      <button onClick={() => setMark((m) => ({ ...m, out: playhead }))}>
                        标出点 <kbd>O</kbd>
                      </button>
                      <button
                        className="ghost"
                        onClick={clearMark}
                        disabled={mark.in == null && mark.out == null}
                      >
                        清空
                      </button>
                    </div>
                    <div className="tool-group">
                      <span className="mark in">入 {mark.in == null ? '—' : fmt(mark.in)}</span>
                      <span className="mark out">出 {mark.out == null ? '—' : fmt(mark.out)}</span>
                      <button
                        onClick={() => mark.in != null && seek(mark.in)}
                        disabled={mark.in == null}
                      >
                        跳到入点
                      </button>
                    </div>
                    <div className="tool-group">
                      <button className="primary" onClick={addClip}>
                        加为片段
                      </button>
                    </div>
                    <div className="tool-group">
                      <button
                        onClick={() => stepClip(-1)}
                        disabled={!clips.length}
                        title="跳到上一段的起点"
                      >
                        ⏮ 上一段 <kbd>←</kbd>
                      </button>
                      <button
                        onClick={() => stepClip(1)}
                        disabled={!clips.length}
                        title="跳到下一段的起点"
                      >
                        下一段 ⏭ <kbd>→</kbd>
                      </button>
                    </div>
                    <div className="tool-group zoom-group">
                      <span className="mark">缩放 {zoom.toFixed(zoom < 10 ? 1 : 0)}×</span>
                      <button className="ghost" onClick={() => zoomBy(1 / 1.6)} disabled={zoom <= ZOOM_MIN}>
                        −
                      </button>
                      <button className="ghost" onClick={() => zoomBy(1.6)} disabled={zoom >= ZOOM_MAX}>
                        ＋
                      </button>
                      <button
                        className="ghost"
                        onClick={() => {
                          setZoom(1)
                          setViewStart(0)
                        }}
                        disabled={zoom === 1}
                      >
                        适应全片
                      </button>
                    </div>
                    <label className="toggle">
                      <input
                        type="checkbox"
                        checked={showSubtitles}
                        onChange={(event) => setShowSubtitles(event.target.checked)}
                      />
                      预览字幕
                    </label>
                  </div>

                  {project.status === 'transcribing' && (
                    <div className="progress">
                      <div className="progress-fill" style={{ width: `${project.progress}%` }} />
                    </div>
                  )}

                  {totalDuration > 0 && (
                    <div className="timeline">
                      {/* 左边的轨名栏，不随缩放平移移动 */}
                      <div className="tl-gutter">
                        <div className="tl-gutter-cell ruler">时间</div>
                        <div className="tl-gutter-cell">字幕</div>
                        <div className="tl-gutter-cell stickers">贴纸</div>
                        <div className="tl-gutter-cell">片段</div>
                      </div>

                      <div className="tl-lanes" ref={lanesRef} onPointerDown={startTimelinePan}>
                        <div className="tl-ruler">
                          {ticks.map((t) => (
                            <div key={t} className="tl-tick" style={{ left: `${toPercent(t)}%` }}>
                              {fmtTick(t)}
                            </div>
                          ))}
                        </div>

                        {/* 字幕轨：拖边缘断句，拖中间整句平移 */}
                        <div className="tl-track tl-track-cue">
                          {subtitles.map((cue, index) =>
                            cue.end > viewStart && cue.start < viewStart + timeWindow ? (
                              <div
                                key={index}
                                className={`tl-block${index === activeIndex ? ' active' : ''}${
                                  index === selected ? ' selected' : ''
                                }`}
                                title={cue.text}
                                style={{
                                  left: `${toPercent(cue.start)}%`,
                                  width: `${Math.max(
                                    0.15,
                                    ((cue.end - cue.start) / (timeWindow || 1)) * 100,
                                  )}%`,
                                }}
                              >
                                <span
                                  className="tl-edge"
                                  onPointerDown={(event) => {
                                    event.stopPropagation()
                                    setSelected(index)
                                    setTimelineDrag({
                                      kind: 'edge',
                                      target: 'cue',
                                      index,
                                      side: 'start',
                                      owner: `cue:${index}`,
                                      start: cue.start,
                                      end: cue.end,
                                    })
                                  }}
                                />
                                <span
                                  className="tl-body"
                                  onPointerDown={(event) => {
                                    event.stopPropagation()
                                    setSelected(index)
                                    setTimelineDrag({
                                      kind: 'move',
                                      target: 'cue',
                                      index,
                                      owner: `cue:${index}`,
                                      grab: positionToTime(event.clientX),
                                      start: cue.start,
                                      end: cue.end,
                                    })
                                  }}
                                />
                                <span
                                  className="tl-edge"
                                  onPointerDown={(event) => {
                                    event.stopPropagation()
                                    setSelected(index)
                                    setTimelineDrag({
                                      kind: 'edge',
                                      target: 'cue',
                                      index,
                                      side: 'end',
                                      owner: `cue:${index}`,
                                      start: cue.start,
                                      end: cue.end,
                                    })
                                  }}
                                />
                              </div>
                            ) : null,
                          )}
                        </div>

                        {/* 贴纸轨：块的长度就是它出现到消失的时间 */}
                        <div className="tl-track tl-track-sticker">
                          {stickers.map((item) => {
                            const end = item.end > 0 ? item.end : totalDuration
                            if (end <= viewStart || item.start >= viewStart + timeWindow) {
                              return null
                            }
                            const openEnd = item.end <= 0
                            return (
                              <div
                                key={item.id}
                                className="tl-sticker"
                                title={`${item.file}（上下拖动换泳道，左右拖中间整体挪，拖两边改出现/消失）`}
                                style={{
                                  top: `${item.track * LANE_HEIGHT}px`,
                                  left: `${toPercent(item.start)}%`,
                                  width: `${Math.max(
                                    0.4,
                                    ((end - item.start) / (timeWindow || 1)) * 100,
                                  )}%`,
                                }}
                              >
                                <span
                                  className="tl-edge"
                                  onPointerDown={(event) => {
                                    event.stopPropagation()
                                    setTimelineDrag({
                                      kind: 'edge',
                                      target: 'sticker',
                                      id: item.id,
                                      side: 'start',
                                      owner: `sticker:${item.id}`,
                                      start: item.start,
                                      end,
                                      openEnd,
                                    })
                                  }}
                                />
                                <span
                                  className="tl-body"
                                  onPointerDown={(event) => {
                                    event.stopPropagation()
                                    setTimelineDrag({
                                      kind: 'move',
                                      target: 'sticker',
                                      id: item.id,
                                      owner: `sticker:${item.id}`,
                                      grab: positionToTime(event.clientX),
                                      start: item.start,
                                      end,
                                      openEnd,
                                    })
                                  }}
                                />
                                <span
                                  className="tl-edge"
                                  onPointerDown={(event) => {
                                    event.stopPropagation()
                                    setTimelineDrag({
                                      kind: 'edge',
                                      target: 'sticker',
                                      id: item.id,
                                      side: 'end',
                                      owner: `sticker:${item.id}`,
                                      start: item.start,
                                      end,
                                      openEnd,
                                    })
                                  }}
                                />
                              </div>
                            )
                          })}
                        </div>

                        {/* 片段轨：拖边缘改起止，拖中间整段平移，点一下选中 */}
                        <div className="tl-track tl-track-clip">
                          {clips.map((clip) => (
                            <div
                              key={clip.id}
                              className={`tl-clip${clip.mirror ? ' mirrored' : ''}${
                                selectedClip === clip.id ? ' selected' : ''
                              }`}
                              title={`${clip.title}（拖中间整段平移，拖两边改起止）`}
                              style={{
                                left: `${toPercent(clip.start)}%`,
                                width: `${Math.max(
                                  0.4,
                                  ((clip.end - clip.start) / (timeWindow || 1)) * 100,
                                )}%`,
                              }}
                            >
                              <span
                                className="tl-edge"
                                onPointerDown={(event) => {
                                  event.stopPropagation()
                                  setSelectedClip(clip.id)
                                  setTimelineDrag({
                                    kind: 'edge',
                                    target: 'clip',
                                    id: clip.id,
                                    side: 'start',
                                    owner: `clip:${clip.id}`,
                                    start: clip.start,
                                    end: clip.end,
                                  })
                                }}
                              />
                              <span
                                className="tl-body"
                                onPointerDown={(event) => {
                                  event.stopPropagation()
                                  setSelectedClip(clip.id)
                                  setTimelineDrag({
                                    kind: 'move',
                                    target: 'clip',
                                    id: clip.id,
                                    owner: `clip:${clip.id}`,
                                    grab: positionToTime(event.clientX),
                                    start: clip.start,
                                    end: clip.end,
                                    // 挤压要能从「按下鼠标那一刻」重算全部片段，所以留一份快照
                                    snapshot: clips,
                                  })
                                }}
                              />
                              <span
                                className="tl-edge"
                                onPointerDown={(event) => {
                                  event.stopPropagation()
                                  setSelectedClip(clip.id)
                                  setTimelineDrag({
                                    kind: 'edge',
                                    target: 'clip',
                                    id: clip.id,
                                    side: 'end',
                                    owner: `clip:${clip.id}`,
                                    start: clip.start,
                                    end: clip.end,
                                  })
                                }}
                              />
                            </div>
                          ))}
                        </div>

                        {mark.in != null && inView(mark.in) && (
                          <div className="tl-mark in" style={{ left: `${toPercent(mark.in)}%` }} />
                        )}
                        {mark.out != null && inView(mark.out) && (
                          <div
                            className="tl-mark out"
                            style={{ left: `${toPercent(mark.out)}%` }}
                          />
                        )}
                        {inView(playhead) && (
                          <div
                            className="tl-playhead"
                            style={{ left: `${toPercent(playhead)}%` }}
                          />
                        )}
                      </div>
                    </div>
                  )}
                </>
              )}
            </>
          ) : (
            <div className="empty">先选一个工程，或者把回放视频直接拖进来</div>
          )}
        </section>

        <div
          className="v-splitter"
          title="左右拖动，调整右侧栏宽度"
          onPointerDown={startSideDrag}
        />

        <aside className="side" style={{ width: sideWidth }}>
          <section className="panel panel-subtitles" style={{ height: panelSizes.subtitles }}>
            <div className="panel-head">
              <h2>字幕</h2>
              <span className="count">{subtitles.length} 条</span>
              <button
                className="primary"
                onClick={saveSubtitlePanel}
                disabled={!dirtySubtitles && !dirtyStyle}
              >
                {dirtySubtitles || dirtyStyle ? '保存改动' : '已保存'}
              </button>
            </div>
            <div className="style-bar">
              <select
                value={style.font}
                onChange={(event) => patchStyle({ font: event.target.value })}
              >
                {fontOptions.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
              <button className="ghost upload" onClick={() => fontFileRef.current?.click()}>
                上传字体
              </button>
              <input
                ref={fontFileRef}
                type="file"
                accept=".ttf,.otf,.ttc"
                hidden
                onChange={handleFontUpload}
              />
              <label className="size">
                字号
                <input
                  type="number"
                  min="8"
                  max="48"
                  step="1"
                  value={style.size}
                  onChange={(event) => patchStyle({ size: Number(event.target.value) })}
                />
              </label>
              <input
                className="swatch"
                type="color"
                value={style.color}
                onChange={(event) => patchStyle({ color: event.target.value })}
              />
              <span className="box-width" title="字幕框宽度：拖画面上下字幕框右下角的方块就能改">
                框宽 {Math.round(style.width * 100)}%
              </span>
            </div>
            <div className="rows" ref={rowsRef}>
              {subtitles.length === 0 && <p className="hint">还没有字幕，点上面的「转写」生成</p>}
              {subtitles.map((cue, index) => (
                <div
                  key={index}
                  data-index={index}
                  className={`row${index === activeIndex ? ' active' : ''}${
                    index === selected ? ' selected' : ''
                  }`}
                  onClick={() => {
                    setSelected(index)
                    seek(cue.start)
                  }}
                >
                  <button
                    className="play"
                    title="跳到这条字幕"
                    onClick={(event) => {
                      event.stopPropagation()
                      seek(cue.start)
                    }}
                  >
                    ▶
                  </button>
                  <span className="times" onClick={(event) => event.stopPropagation()}>
                    <TimeInput value={cue.start.toFixed(1)} onCommit={(v) => patchCue(index, { start: v })} />
                    <TimeInput value={cue.end.toFixed(1)} onCommit={(v) => patchCue(index, { end: v })} />
                  </span>
                  <input
                    className="text"
                    value={cue.text}
                    onClick={(event) => event.stopPropagation()}
                    onChange={(event) => patchCue(index, { text: event.target.value })}
                  />
                </div>
              ))}
            </div>
          </section>

          <div
            className="splitter splitter-a"
            title="按住上下拖，调整「字幕」和「片段」的高度"
            onPointerDown={(event) => startSplitDrag(event, 'subtitles', 'clips')}
          />

          <section className="panel panel-decor" style={{ height: panelSizes.decor }}>
            <div className="panel-head">
              <h2>装饰</h2>
              <span className="count">{stickers.length} 张贴纸</span>
              <button onClick={() => stickerFileRef.current?.click()}>上传贴纸</button>
              <input
                ref={stickerFileRef}
                type="file"
                accept="image/png,image/jpeg,image/webp,image/gif"
                hidden
                onChange={handleStickerUpload}
              />
            </div>
            <div className="stickers">
              {stickers.length === 0 && (
                <p className="hint">
                  传一张透明 PNG 当贴纸（水印、角标、活动标签），位置和大小会同时用在预览和成片上。
                </p>
              )}
              {stickers.map((item) => (
                <StickerRow
                  key={item.id}
                  sticker={item}
                  url={api.stickerUrl(project.id, item.file)}
                  onPatch={(patch) => patchSticker(item.id, patch)}
                  onRemove={() => removeSticker(item)}
                />
              ))}
            </div>
          </section>

          <div
            className="splitter splitter-b"
            title="按住上下拖，调整「片段」和「装饰」的高度"
            onPointerDown={(event) => startSplitDrag(event, 'clips', 'decor')}
          />

          <section className="panel panel-clips" style={{ height: panelSizes.clips }}>
            <div className="panel-head">
              <h2>片段</h2>
              <span className="count">{clips.length} 个</span>
              <label className="limit" title="AI 一次最多挑几段，填 0 表示不限">
                高光
                <input
                  type="number"
                  min="0"
                  max="50"
                  value={highlightLimit}
                  onChange={(event) => setHighlightLimit(Number(event.target.value))}
                />
                段
              </label>
              <button
                onClick={handleAnalyze}
                disabled={!project || busy || project.status === 'analyzing' || !subtitles.length}
                title="让模型读转写稿，找出正在介绍商品的片段"
              >
                AI 找高光
              </button>
              <button onClick={saveClips} disabled={!dirtyClips}>
                {dirtyClips ? '保存改动' : '已保存'}
              </button>
              <button
                onClick={handleConcatPreview}
                disabled={!project || busy || project.status === 'previewing' || !clips.length}
                title="把所有片段串成一条片，导出前先整体过一遍"
              >
                拼接预览
              </button>
              <button
                className="primary"
                onClick={handleRender}
                disabled={!project || busy || project.status === 'rendering'}
              >
                导出切片
              </button>
            </div>
            <div className="clip-ops">
              <button onClick={splitClip} disabled={!selectedClip}>
                在播放头处剪开
              </button>
              <button onClick={mergeClip} disabled={!selectedClip}>
                与下一段合并
              </button>
              <span className="note">
                {selectedClip ? '已选中一段' : '点一段选中后可以剪开或合并'}
              </span>
            </div>
            {project?.segments?.length > 0 && (
              <div className="ai-list">
                <div className="ai-head">
                  <span>AI 找到 {project.segments.length} 段商品讲解</span>
                  <button className="ghost" onClick={addAllSegments}>
                    全部加为片段
                  </button>
                </div>
                {project.segments.map((segment) => {
                  const pick = segment.clips[0]
                  return (
                    <div key={segment.id} className="ai-item">
                      <div className="ai-row1">
                        <span className="ai-name">{segment.name}</span>
                        <span className="ai-range">
                          {fmt(segment.start)} → {fmt(segment.end)}
                        </span>
                        <button className="ghost" onClick={() => addSegment(segment)}>
                          加为片段
                        </button>
                      </div>
                      {pick && (
                        <div className="ai-row2">
                          {pick.title}
                          {pick.reason ? ` · ${pick.reason}` : ''}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
            <div className="rows">
              {clips.length === 0 && (
                <p className="hint">播放到想要的位置，标入点、出点，再加为片段</p>
              )}
              <DndContext
                sensors={sensors}
                collisionDetection={closestCenter}
                onDragEnd={handleDragEnd}
              >
                <SortableContext
                  items={clips.map((clip) => clip.id)}
                  strategy={verticalListSortingStrategy}
                >
                  {clips.map((clip, index) => (
                    <SortableClip
                      key={clip.id}
                      clip={clip}
                      index={index}
                      count={clips.length}
                      selected={selectedClip === clip.id}
                      onSelect={() => setSelectedClip(clip.id)}
                      onPatch={patchClip}
                      onRemove={removeClip}
                      onSeek={seek}
                      onMove={moveClip}
                    />
                  ))}
                </SortableContext>
              </DndContext>
            </div>
            {project?.renders?.length > 0 && (
              <div className="rendered">
                <h3>已导出</h3>
                {project.renders.map((item) => (
                  <a key={item.file} href={api.renderUrl(project.id, item.file)} download>
                    {item.title || item.file}
                  </a>
                ))}
                <button className="ghost reveal" onClick={handleReveal}>
                  在文件夹中打开
                </button>
              </div>
            )}
          </section>
        </aside>
      </div>

      {showSettings && settings && (
        <div className="modal-backdrop" onClick={() => setShowSettings(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <h2>模型设置</h2>

            <section>
              <h3>文本模型 · 找商品讲解段、起标题</h3>
              <label>
                接口地址
                <input
                  value={settings.llm.base_url}
                  placeholder="https://.../v1"
                  onChange={(event) => patchSettings('llm', { base_url: event.target.value })}
                />
              </label>
              <label>
                密钥
                <input
                  value={settings.llm.api_key}
                  placeholder="留空表示不修改"
                  onChange={(event) => patchSettings('llm', { api_key: event.target.value })}
                />
              </label>
              <label>
                模型名
                <input
                  value={settings.llm.model}
                  placeholder="mimo-v2.6-flash"
                  onChange={(event) => patchSettings('llm', { model: event.target.value })}
                />
              </label>
            </section>

            <section>
              <h3>语音识别 · 把回放转成字幕</h3>
              <label>
                走哪条路
                <select
                  value={settings.asr.provider}
                  onChange={(event) => patchSettings('asr', { provider: event.target.value })}
                >
                  <option value="cloud">云端接口</option>
                  <option value="llama">本地 llama.cpp（Qwen3-ASR）</option>
                </select>
              </label>
              {settings.asr.provider === 'cloud' ? (
                <>
                  <label>
                    接口地址
                    <input
                      value={settings.asr.base_url}
                      onChange={(event) => patchSettings('asr', { base_url: event.target.value })}
                    />
                  </label>
                  <label>
                    密钥
                    <input
                      value={settings.asr.api_key}
                      placeholder="留空表示不修改"
                      onChange={(event) => patchSettings('asr', { api_key: event.target.value })}
                    />
                  </label>
                  <label>
                    模型名
                    <input
                      value={settings.asr.model}
                      onChange={(event) => patchSettings('asr', { model: event.target.value })}
                    />
                  </label>
                </>
              ) : (
                <>
                  <label>
                    llama-server 程序
                    <input
                      value={settings.asr.llama_bin}
                      placeholder="留空 = vendor/llama/llama-server.exe"
                      onChange={(event) => patchSettings('asr', { llama_bin: event.target.value })}
                    />
                  </label>
                  <label>
                    模型 GGUF
                    <input
                      value={settings.asr.llama_model}
                      placeholder="留空 = vendor/models/Qwen3-ASR-0.6B-Q8_0.gguf"
                      onChange={(event) => patchSettings('asr', { llama_model: event.target.value })}
                    />
                  </label>
                  <label>
                    音频编码器 GGUF
                    <input
                      value={settings.asr.llama_mmproj}
                      placeholder="留空 = vendor/models/mmproj-Qwen3-ASR-0.6B-Q8_0.gguf"
                      onChange={(event) => patchSettings('asr', { llama_mmproj: event.target.value })}
                    />
                  </label>
                  <label>
                    端口
                    <input
                      type="number"
                      value={settings.asr.llama_port}
                      onChange={(event) =>
                        patchSettings('asr', { llama_port: Number(event.target.value) })
                      }
                    />
                  </label>
                  <p className="modal-note">
                    第一次转写时后端会自己把 llama-server 拉起来，之后一直复用这个进程。
                    GGUF 是 mmap 加载的，权重几乎不计入系统提交量，所以不会像以前
                    transformers 那条路一样把内存顶穿。实测约 0.35 倍实时，比云端快。
                  </p>
                </>
              )}
            </section>

            <section>
              <h3>导出 · 成片落到哪里</h3>
              <label>
                导出目录
                <input
                  value={settings.export.dir}
                  placeholder="留空 = 存在工程自己的 renders 目录"
                  onChange={(event) => patchSettings('export', { dir: event.target.value })}
                />
              </label>
              <p className="modal-note">
                填了的话，渲染完成后会把成片再复制一份到这个目录，方便直接拿去发布。
              </p>
            </section>

            <div className="modal-actions">
              <button onClick={() => setShowSettings(false)}>取消</button>
              <button className="primary" onClick={saveSettings}>
                保存
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
