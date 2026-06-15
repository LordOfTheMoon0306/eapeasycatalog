import { useEffect, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import Loader from '../components/Loader'
import Toast from '../components/Toast'
import { api } from '../services/api'

const PRODUCT_SNAPSHOT_PREFIX = 'product:'

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

const productStorageKey = (source, productUrl) => `${PRODUCT_SNAPSHOT_PREFIX}${source}:${productUrl}`

const readProductFromSessionStorage = (source, productUrl) => {
  if (!source || !productUrl) {
    return null
  }

  try {
    const raw = sessionStorage.getItem(productStorageKey(source, productUrl))
    if (!raw) {
      return null
    }

    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

const normalizeProductSnapshot = (product, fallbackSource, fallbackProductUrl) => {
  if (!product || typeof product !== 'object') {
    return null
  }

  const productUrl = product.product_url || product.url || fallbackProductUrl
  if (!productUrl) {
    return null
  }

  return {
    ...product,
    source: String(product.source || fallbackSource || '').toLowerCase(),
    product_url: productUrl,
    image_url: product.image_url || product.image || null,
  }
}

const digitsOnly = (value) => String(value || '').replace(/\D/g, '')

const isValidPrice = (value) => {
  if (!value || typeof value !== 'string') {
    return false
  }

  const digits = digitsOnly(value)
  if (digits.length < 4) {
    return false
  }

  const numeric = Number(digits)
  if (!Number.isFinite(numeric) || numeric < 1_000 || numeric > 100_000_000) {
    return false
  }

  return /₸|тг|тенге/i.test(value) || /\d[\d\s\xa0]{3,}/.test(value)
}

const isValidRating = (value) => {
  if (value === null || value === undefined || value === '') {
    return false
  }

  const numeric = Number(String(value).replace(',', '.'))
  return Number.isFinite(numeric) && numeric >= 0 && numeric <= 5
}

const isValidReviewsCount = (value, price) => {
  if (value === null || value === undefined || value === '') {
    return false
  }

  const digits = digitsOnly(value)
  if (!digits) {
    return false
  }

  const numeric = Number(digits)
  if (!Number.isFinite(numeric) || numeric > 50_000) {
    return false
  }

  const priceDigits = digitsOnly(price)
  if (priceDigits && digits === priceDigits) {
    return false
  }

  return true
}

const chooseKaspiField = (initialValue, detailValue, validator = (value) => Boolean(value), context) => {
  if (initialValue) {
    return initialValue
  }
  return validator(detailValue, context) ? detailValue : initialValue || null
}

const mergeProductDetails = (initialProduct, detail, source, productUrl) => {
  const normalizedInitial = normalizeProductSnapshot(initialProduct, source, productUrl)
  const normalizedDetail = normalizeProductSnapshot(detail, source, productUrl)

  if (String(source || '').toLowerCase() !== 'kaspi') {
    return {
      ...(normalizedInitial || {}),
      ...(normalizedDetail || {}),
      product_url: normalizedDetail?.product_url || normalizedInitial?.product_url || productUrl,
    }
  }

  const base = {
    ...(normalizedDetail || {}),
    ...(normalizedInitial || {}),
    description: normalizedDetail?.description,
    characteristics: normalizedDetail?.characteristics || {},
    raw_sections: normalizedDetail?.raw_sections || {},
    product_url: normalizedInitial?.product_url || normalizedDetail?.product_url || productUrl,
    seller: normalizedInitial?.seller || normalizedDetail?.seller || null,
  }

  return {
    ...base,
    title: normalizedInitial?.title || normalizedDetail?.title || '',
    image_url: normalizedInitial?.image_url || normalizedDetail?.image_url || null,
    price: chooseKaspiField(normalizedInitial?.price, normalizedDetail?.price, isValidPrice),
    rating: chooseKaspiField(normalizedInitial?.rating, normalizedDetail?.rating, isValidRating),
    reviews_count: chooseKaspiField(
      normalizedInitial?.reviews_count,
      normalizedDetail?.reviews_count,
      isValidReviewsCount,
      normalizedInitial?.price || normalizedDetail?.price,
    ),
  }
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
  const location = useLocation()
  const source = String(params.get('source') || '').toLowerCase()
  const productUrl = params.get('url')
  const initialProduct = normalizeProductSnapshot(
    location.state?.product || readProductFromSessionStorage(source, productUrl),
    source,
    productUrl,
  )

  const [loading, setLoading] = useState(false)
  const [item, setItem] = useState(initialProduct)
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
          setItem(mergeProductDetails(initialProduct, cached, source, productUrl))
          setError('')
          setLoading(false)
        }
        return
      }

      setItem(initialProduct)
      setLoading(true)
      try {
        const payload = await api.productDetails(source, productUrl)
        if (mounted) {
          setItem(mergeProductDetails(initialProduct, payload, source, productUrl))
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

      {!loading && error && !item && (
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

      {item && (
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
