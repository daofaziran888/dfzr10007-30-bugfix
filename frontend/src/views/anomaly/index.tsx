import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/anomaly'
const columns = [
  "记录编号",
  "指标名称",
  "异动方向",
  "异动幅度",
  "规则编号",
  "通知渠道",
  "静默周期",
  "归因状态",
  "告警状态",
  "静默状态",
  "静默剩余分钟"
]
const detailFields = [
  "记录编号",
  "指标名称",
  "异动方向",
  "异动幅度",
  "归因维度",
  "归因结论",
  "处理建议",
  "异动时间",
  "规则编号",
  "规则状态",
  "告警级别",
  "通知渠道",
  "静默周期",
  "告警状态",
  "静默状态",
  "静默剩余分钟",
  "触发时间",
  "静默截止时间",
  "上次触发时间",
  "距上次触发分钟",
  "静默校验",
  "触发来源"
]
const actions = [
  "开始分析",
  "确认归因",
  "关闭异动"
]
const statuses = [
  "待分析",
  "分析中",
  "已归因",
  "已关闭"
]

type Row = Record<string, string | number | null>

function display(value: Row[string]): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

/** “归因状态”读的是归因工作流字段 status；“告警状态”是告警规则写入的独立字段。 */
function cellValue(row: Row, column: string): Row[string] {
  return column === '归因状态' ? row.status : row[column]
}

export default function AnomalyView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [detail, setDetail] = useState<Row | null>(null)
  const filterFields = useMemo(() => ["记录编号", "指标名称", "异动方向"], [])

  const stats = useMemo(() => [
    { label: '异动记录总数', value: total },
    { label: '告警规则触发', value: rows.filter((row) => row["规则编号"]).length },
    { label: '静默中记录', value: rows.filter((row) => row["静默状态"] === '静默中').length },
  ], [rows, total])
  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('异动记录列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '指标异动归因列表读取失败')
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
        throw new Error('指标异动归因动作未生效，请稍后重试')
      }
      const payload = await response.json()
      if (!payload.ok) {
        setErrorMessage(String(payload.message ?? '指标异动归因动作未生效'))
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '指标异动归因操作失败')
    }
  }

  async function openDetail(row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}`)
      if (!response.ok) throw new Error('异动记录详情读取失败')
      setDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '异动记录详情读取失败')
    }
  }

  return (
    <section className="page" data-module="anomaly">
      <header className="page-head">
        <div>
          <h2>指标异动归因管理</h2>
          <p className="page-desc">维护指标异动的方向识别、维度归因与处理建议；告警规则触发的记录在这里带出通知渠道与静默周期。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('异动记录登记入口尚未接入审批流')}>
            登记异动记录
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出指标异动归因清单
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
          <span>归因状态</span>
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
                <td key={column} className={column === '静默状态' && cellValue(row, column) === '静默中' ? 'state-silenced' : undefined}>
                  {display(cellValue(row, column))}
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无指标异动归因数据，可先登记异动记录</td>
            </tr>
          )}
        </tbody>
      </table>

      {detail && (
        <aside className="detail-panel" data-panel="anomaly-detail">
          <header className="detail-head">
            <h3>异动详情 · {display(detail["记录编号"])}</h3>
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
        <span>共 {total} 条指标异动归因记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
