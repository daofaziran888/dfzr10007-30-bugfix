import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/alarm'
const columns = [
  "规则编号",
  "监控指标",
  "告警条件",
  "通知渠道",
  "静默周期",
  "规则状态",
  "触发结果",
  "静默状态"
]
const detailFields = [
  "规则编号",
  "监控指标",
  "告警条件",
  "告警级别",
  "通知渠道",
  "静默周期",
  "生效时段",
  "规则状态",
  "触发结果",
  "静默状态",
  "静默剩余分钟",
  "静默截止时间",
  "最近触发时间",
  "最近触发记录",
  "触发次数",
  "条件求值结果",
  "触发说明"
]
const actions = [
  "启用规则",
  "暂停规则",
  "关闭规则"
]
const statuses = [
  "草稿",
  "已启用",
  "已暂停",
  "已关闭"
]

type Row = Record<string, string | number | null | boolean | string[]>

function display(value: Row[string]): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join('、') : '—'
  return String(value)
}

export default function AlarmView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Row | null>(null)
  const filterFields = useMemo(() => ["规则编号", "监控指标", "告警条件"], [])

  const stats = useMemo(() => [
    { label: '告警规则总数', value: total },
    { label: '已启用规则', value: rows.filter((row) => row["规则状态"] === '已启用').length },
    { label: '静默中规则', value: rows.filter((row) => row["静默状态"] === '静默中').length },
  ], [rows, total])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('告警规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析告警规则列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('分析告警规则动作未生效，请稍后重试')
      }
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(String(payload.message ?? '分析告警规则动作未生效'))
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析告警规则操作失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) throw new Error('告警规则详情读取失败')
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '告警规则详情读取失败')
    }
  }

  return (
    <section className="page" data-module="alarm">
      <header className="page-head">
        <div>
          <h2>分析告警规则管理</h2>
          <p className="page-desc">维护分析告警的监控指标、触发条件与通知策略；规则状态、触发结果与静默周期状态在此保持一致。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('告警规则登记入口尚未接入审批流')}>
            登记告警规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析告警规则清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>规则状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column} className={column === '静默状态' && row[column] === '静默中' ? 'state-silenced' : undefined}>
                  {display(row[column])}
                </td>
              ))}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => void openDetail(row)}>查看详情</button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析告警规则数据，可先登记告警规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <aside className="detail-panel" data-panel="alarm-detail">
          <header className="detail-head">
            <h3>告警规则详情 · {display(detail["规则编号"])}</h3>
            <button className="btn ghost" type="button" onClick={() => setDetail(null)}>关闭</button>
          </header>
          <dl className="detail-grid">
            {detailFields.map((field) => (
              <div key={field} className="detail-item">
                <dt>{field}</dt>
                <dd>{display(detail[field])}</dd>
              </div>
            ))}
          </dl>
        </aside>
      )}

      <footer className="page-foot">
        <span>共 {total} 条分析告警规则记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
