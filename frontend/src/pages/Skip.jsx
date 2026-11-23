import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api'

const EMPTY = { 已作废: 0, 办理中: 0, 过号: 0 }

export default function Skip() {
  const [skipped, setSkipped] = useState([])
  const [wins, setWins] = useState([])
  const [wid, setWid] = useState('')
  const [overview, setOverview] = useState(null)
  const [wall, setWall] = useState(null)
  const [events, setEvents] = useState([])
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [busyId, setBusyId] = useState(null)
  const [replay, setReplay] = useState(null)
  const inflight = useRef(false)

  // 一次动作后，回收列表 / 等候墙 / 叫号事件 / 窗口指针 / 总览两桶一起重取。
  const load = useCallback(async () => {
    const [t, w, o, wallData, e] = await Promise.all([
      api.tickets('过号'),
      api.windows(),
      api.overview(),
      api.wall(),
      api.events(),
    ])
    setSkipped(t)
    setWins(w.filter(x => x.开放))
    setOverview(o)
    setWall(wallData)
    setEvents(e)
    setWid(prev => prev || String(w.find(x => x.开放)?.id || ''))
  }, [])

  useEffect(() => { load().catch(e => setErr(e.message)) }, [load])

  async function act(id, fn, label) {
    if (inflight.current) return
    inflight.current = true
    setBusyId(id)
    setErr('')
    setMsg('')
    try {
      const receipt = await fn(id)
      // 回执直接取自动作事务内落库结果：号牌终态、窗口指针、事件同源。
      setMsg(
        `${label} ${receipt.号牌} → ${receipt.状态}` +
        ` · 窗口指针 ${receipt.窗口指针 ?? '空'} · 事件 #${receipt.事件?.id ?? '-'}`
      )
      await load()
    } catch (e) {
      setErr(e.message)
      // 失败也可能已改动段前状态，重取一次确保界面不留在半截。
      await load().catch(() => {})
    } finally {
      inflight.current = false
      setBusyId(null)
    }
  }

  const onRecall = (id) => act(id, (tid) => api.recall(tid, Number(wid)), '已重叫')
  const onVoid = (id) => act(id, (tid) => api.voidTicket(tid), '已作废')
  const onReplay = async (id) => {
    setErr('')
    try { setReplay(await api.replay(id)) } catch (e) { setErr(e.message) }
  }

  const o = overview || EMPTY
  const wallSkipped = wall?.墙面?.reduce((n, b) => n + b.过号数, 0) ?? 0
  const wallWaiting = wall?.墙面?.reduce((n, b) => n + b.等候数, 0) ?? 0

  return (
    <div className="page">
      <header className="hero">
        <p className="eyebrow">回收命运</p>
        <h1>过号重叫</h1>
        <p className="lede">
          过号可重叫回窗办理，或作废另取。作废为准：号牌一旦作废，重叫整笔拒绝；
          重叫命中即占窗办理，此后再作废亦然拒绝。两种结局互斥，不会同时成立。
        </p>
      </header>

      <section className="panel">
        <div className="form row">
          <label>重叫窗口
            <select value={wid} onChange={e => setWid(e.target.value)}>
              {wins.map(w => <option key={w.id} value={w.id}>{w.名称}</option>)}
            </select>
          </label>
        </div>
        {err && <p className="err">{err}</p>}
        {msg && <p className="okmsg">{msg}</p>}
        <div className="card-grid" style={{ marginTop: 12 }}>
          {skipped.length === 0 && <p className="muted">暂无过号</p>}
          {skipped.map(t => (
            <div className="slip" key={t.id}>
              <div className="no">{t.号牌}</div>
              <div className="meta">{t.业务种类} · {t.状态}</div>
              <div className="actions">
                <button
                  type="button"
                  disabled={busyId !== null}
                  onClick={() => onRecall(t.id)}
                >
                  {busyId === t.id ? '处理中…' : '重叫'}
                </button>
                <button
                  type="button"
                  className="danger"
                  disabled={busyId !== null}
                  onClick={() => onVoid(t.id)}
                >
                  {busyId === t.id ? '处理中…' : '作废'}
                </button>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="panel">
        <h2>联动快照</h2>
        <p className="muted">
          回收列表 {skipped.length} · 等候墙 过号 {wallSkipped} / 等候 {wallWaiting}
          {' · '}总览 作废 {o.已作废} / 办理中 {o.办理中} / 过号 {o.过号}
        </p>
        <ul className="plain">
          {events.slice(0, 6).map(e => (
            <li key={e.id}>
              <button
                type="button"
                className="ghost"
                style={{ padding: '2px 8px', marginRight: 8 }}
                onClick={() => onReplay(e.号牌编号)}
              >
                回放
              </button>
              {e.时刻} · {e.窗口 || '—'} · {e.动作} · {e.号牌}
              {e.前态 ? ` · ${e.前态}→${e.后态}` : ''}
            </li>
          ))}
          {!events.length && <li className="muted">暂无叫号事件</li>}
        </ul>
        {replay && (
          <div style={{ marginTop: 12 }}>
            <h3>
              {replay.号牌} · 当前 {replay.当前状态} ·{' '}
              <span className={replay.闭合 ? 'okmsg' : 'err'}>{replay.说明}</span>
            </h3>
            <ul className="plain">
              {replay.事件.map(step => (
                <li key={step.序}>
                  {step.序}. {step.时刻} · {step.动作} · {step.前态 || '—'}→{step.后态 || '—'}
                  {step.窗口编号 ? ` · 窗${step.窗口编号}` : ''}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  )
}
