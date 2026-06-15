import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import Loader from '../components/Loader'
import Toast from '../components/Toast'
import { api } from '../services/api'

const splitReadableLines = (value) => {
  if (!value || typeof value !== 'string') {
    return []
  }

  const normalized = value
    .replace(/\u00a0/g, ' ')
    .replace(/\s*•\s*/g, '\n• ')
    .replace(/\s*;\s*/g, ';\n')
    .replace(/\s{2,}/g, ' ')
    .trim()

  if (!normalized) {
    return []
  }

  return normalized
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean)
}

const formatReviews = (value) => {
  if (!value) {
    return 'Нет данных'
  }

  return `${value} отзывов`
}

const normalizeRawSections = (rawSections) => {
  if (!rawSections || typeof rawSections !== 'object') {
    return []
  }

  return Object.entries(rawSections)
    .map(([key, values]) => {
      if (!Array.isArray(values)) {
        return [key, []]
      }

      const deduped = [...new Set(values.map((v) => `${v || ''}`.trim()).filter(Boolean))]
      return [key, deduped.slice(0, 16)]
    })
    .filter(([, values]) => values.length > 0)
}

export default function ProductDetailsPage() {
  const [params] = useSearchParams()
  const source = params.get('source')
  const productUrl = params.get('url')

  const [loading, setLoading] = useState(false)
  const [item, setItem] = useState(null)
  const [error, setError] = useState('')
  const descriptionLines = splitReadableLines(item?.description)
  const extraSections = item?.source === 'ozon' ? [] : normalizeRawSections(item?.raw_sections)

  useEffect(() => {
    let mounted = true
    const load = async () => {
      if (!source || !productUrl) {
        setError('Не переданы параметры товара')
        setLoading(false)
        return
      }

      const cached = api.getCachedProductDetails(source, productUrl)
      if (cached) {
        if (mounted) {
          setItem(cached)
          setError('')
          setLoading(false)
        }
        return
      }

      setItem(null)
      setLoading(true)
      try {
        const payload = await api.productDetails(source, productUrl)
        if (mounted) {
          setItem(payload)
          setError('')
        }
      } catch (e) {
        if (mounted) {
          setError(e.message)
        }
      } finally {
        if (mounted) {
          setLoading(false)
        }
      }
    }

    load()
    return () => {
      mounted = false
    }
  }, [source, productUrl])

  return (
    <main className="page details-page">
      <Link to="/" className="back-link">
        Назад к поиску
      </Link>

      {loading && <Loader text="Загружаем карточку товара..." />}

      {!loading && error && (
        <section className="details-card">
          <header>
            <h1>Product details unavailable</h1>
            <p>{source}</p>
          </header>
          <p className="source-error">{error}</p>
          {productUrl && (
            <a href={productUrl} target="_blank" rel="noreferrer noopener">
              Open original product page
            </a>
          )}
        </section>
      )}

      {!loading && !error && item && (
        <article className="details-card">
          <header>
            <h1>{item.title || 'Без названия'}</h1>
            <p>{item.source}</p>
          </header>

          {item.image_url && <img src={item.image_url} alt={item.title} className="details-image" />}

          <section className="details-main">
            <div>
              <h3>Цена</h3>
              <p className="details-value">{item.price || 'Не указана'}</p>
            </div>
            <div>
              <h3>Рейтинг</h3>
              <p className="details-value">{item.rating || 'Нет данных'}</p>
            </div>
            <div>
              <h3>Отзывы</h3>
              <p className="details-value">{formatReviews(item.reviews_count)}</p>
            </div>
            <div>
              <h3>Ссылка на источник</h3>
              <a href={item.product_url} target="_blank" rel="noreferrer noopener">
                Открыть страницу товара
              </a>
            </div>
          </section>

          <section className="details-text-section">
            <h2>Описание</h2>
            {descriptionLines.length === 0 && <p className="muted-line">Описание не найдено</p>}
            {descriptionLines.length > 0 && (
              <div className="details-description">
                {descriptionLines.map((line) => (
                  <p key={line}>{line}</p>
                ))}
              </div>
            )}
          </section>

          <section className="details-text-section">
            <h2>Характеристики</h2>
            {Object.keys(item.characteristics || {}).length === 0 && (
              <p className="muted-line">Нет структурированных характеристик</p>
            )}
            <div className="spec-grid">
              {Object.entries(item.characteristics || {}).map(([key, value]) => (
                <div key={key} className="spec-item">
                  <strong>{key}</strong>
                  <span className="spec-value">{value}</span>
                </div>
              ))}
            </div>
          </section>

          {item.source !== 'ozon' && (
            <section className="details-text-section">
            <h2>Дополнительные секции</h2>
            {extraSections.length === 0 && <p className="muted-line">Нет дополнительных секций</p>}
            {extraSections.map(([key, values]) => (
              <div key={key} className="raw-section">
                <h4>{key}</h4>
                <ul className="raw-list">
                  {values.map((v) => (
                    <li key={`${key}-${v}`}>{v}</li>
                  ))}
                </ul>
              </div>
            ))}
            </section>
          )}
        </article>
      )}

      <Toast message={error} onClose={() => setError('')} />
    </main>
  )
}
