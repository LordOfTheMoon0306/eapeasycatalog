import { useEffect, useState } from 'react'
import { api } from '../services/api'

export default function ProxyPanel({ notify }) {
  const [proxiesText, setProxiesText] = useState('')
  const [status, setStatus] = useState(null)
  const [errors, setErrors] = useState([])
  const [saving, setSaving] = useState(false)

  const loadStatus = async () => {
    try {
      const [nextStatus, nextErrors] = await Promise.all([api.proxyStatus(), api.proxyErrors()])
      setStatus(nextStatus)
      setErrors(nextErrors)
    } catch (error) {
      notify(`Ошибка статуса прокси: ${error.message}`)
    }
  }

  useEffect(() => {
    loadStatus()
  }, [])

  const handleTextUpload = async () => {
    if (!proxiesText.trim()) {
      return
    }
    setSaving(true)
    try {
      const loaded = await api.uploadProxiesText(proxiesText)
      notify(`Загружено прокси: ${loaded.length}`)
      await loadStatus()
    } catch (error) {
      notify(`Не удалось загрузить прокси: ${error.message}`)
    } finally {
      setSaving(false)
    }
  }

  const handleFileUpload = async (event) => {
    const file = event.target.files?.[0]
    if (!file) {
      return
    }
    setSaving(true)
    try {
      const loaded = await api.uploadProxiesFile(file)
      notify(`Загружено прокси из файла: ${loaded.length}`)
      await loadStatus()
    } catch (error) {
      notify(`Ошибка загрузки файла: ${error.message}`)
    } finally {
      setSaving(false)
      event.target.value = ''
    }
  }

  const toggleProxy = async () => {
    if (!status) {
      return
    }
    setSaving(true)
    try {
      const next = await api.toggleProxy(!status.enabled)
      setStatus(next)
      notify(`Прокси ${next.enabled ? 'включены' : 'выключены'}`)
    } catch (error) {
      notify(`Не удалось переключить прокси: ${error.message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="proxy-panel">
      <header>
        <h2>Панель прокси</h2>
        <button type="button" className={`toggle ${status?.enabled ? 'on' : ''}`} onClick={toggleProxy}>
          {status?.enabled ? 'ON' : 'OFF'}
        </button>
      </header>

      <div className="proxy-grid">
        <div>
          <p>Вставьте список прокси (по одному на строку):</p>
          <textarea
            value={proxiesText}
            onChange={(event) => setProxiesText(event.target.value)}
            placeholder={'ip:port\nlogin:pass@ip:port'}
            rows={6}
          />
          <div className="proxy-actions">
            <button type="button" onClick={handleTextUpload} disabled={saving}>
              Загрузить из текста
            </button>
            <label className="file-upload">
              Загрузить txt
              <input type="file" accept=".txt" onChange={handleFileUpload} />
            </label>
          </div>
        </div>

        <div className="proxy-status">
          <h3>Состояние</h3>
          <p>Всего: {status?.total ?? '-'}</p>
          <p>Активных: {status?.active ?? '-'}</p>
          <p>Нерабочих: {status?.dead ?? '-'}</p>
          <p>Индекс ротации: {status?.current_index ?? '-'}</p>
          <button type="button" className="ghost" onClick={loadStatus}>
            Обновить
          </button>
        </div>
      </div>

      <div className="proxy-errors">
        <h3>Ошибки прокси</h3>
        {errors.length === 0 && <p>Пока без ошибок</p>}
        {errors.map((entry, idx) => (
          <div key={`${entry.occurred_at}-${idx}`} className="error-item">
            <strong>{entry.reason}</strong>
            <span>{entry.proxy || 'без прокси'}</span>
            <small>{entry.url || '-'}</small>
          </div>
        ))}
      </div>
    </section>
  )
}
