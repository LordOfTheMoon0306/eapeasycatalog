import { useEffect, useMemo, useState } from 'react'
import Loader from '../components/Loader'
import ProductCard from '../components/ProductCard'
import ProxyPanel from '../components/ProxyPanel'
import SearchBar from '../components/SearchBar'
import Tabs from '../components/Tabs'
import Toast from '../components/Toast'
import { api } from '../services/api'

const LAST_SEARCH_PAYLOAD_KEY = 'lastSearchPayload'
const LAST_SEARCH_QUERY_KEY = 'lastSearchQuery'
const LAST_ACTIVE_TAB_KEY = 'lastActiveTab'
const SOURCES = ['kaspi', 'wildberries', 'ozon']
const SORT_MODES = ['default', 'price_asc', 'price_desc']

const createEmptyResults = () => ({ kaspi: [], wildberries: [], ozon: [] })
const createEmptyCounts = () => ({ kaspi: 0, wildberries: 0, ozon: 0 })
const createEmptySourceModes = () => ({ kaspi: 'server', wildberries: 'server', ozon: 'server' })
const createEmptySourceMeta = () => ({
  kaspi: { sellers: [], sellersFound: 0, sellersKnownItems: 0 },
  wildberries: { sellers: [], sellersFound: 0, sellersKnownItems: 0 },
  ozon: { sellers: [], sellersFound: 0, sellersKnownItems: 0 },
})

const normalizeResults = (raw) => {
  const normalized = createEmptyResults()
  if (!raw || typeof raw !== 'object') {
    return normalized
  }

  SOURCES.forEach((source) => {
    if (Array.isArray(raw[source])) {
      normalized[source] = raw[source]
    }
  })

  return normalized
}

const normalizeSourceErrors = (raw) => {
  if (!raw || typeof raw !== 'object') {
    return {}
  }

  const normalized = {}
  SOURCES.forEach((source) => {
    if (typeof raw[source] === 'string' && raw[source].trim()) {
      normalized[source] = raw[source]
    }
  })
  return normalized
}

const normalizeTotalCounts = (raw) => {
  const normalized = createEmptyCounts()
  if (!raw || typeof raw !== 'object') {
    return normalized
  }

  SOURCES.forEach((source) => {
    const value = raw[source]
    if (Number.isFinite(value) && value >= 0) {
      normalized[source] = value
    }
  })

  return normalized
}

const normalizeSourceMeta = (raw) => {
  const normalized = createEmptySourceMeta()
  if (!raw || typeof raw !== 'object') {
    return normalized
  }

  SOURCES.forEach((source) => {
    const entry = raw[source]
    if (!entry || typeof entry !== 'object') {
      return
    }

    const sellers = Array.isArray(entry.sellers)
      ? entry.sellers.filter((value) => typeof value === 'string' && value.trim())
      : []

    const sellersFound = Number.isFinite(entry.sellersFound)
      ? entry.sellersFound
      : Number.isFinite(entry.sellers_unique_count)
        ? entry.sellers_unique_count
        : sellers.length

    const sellersKnownItems = Number.isFinite(entry.sellersKnownItems)
      ? entry.sellersKnownItems
      : Number.isFinite(entry.sellers_known_items)
        ? entry.sellers_known_items
        : sellers.length

    normalized[source] = {
      sellers,
      sellersFound,
      sellersKnownItems,
    }
  })

  return normalized
}

const normalizeSourceModes = (raw) => {
  const normalized = createEmptySourceModes()
  if (!raw || typeof raw !== 'object') {
    return normalized
  }

  SOURCES.forEach((source) => {
    const mode = raw[source]
    normalized[source] = mode === 'client' ? 'client' : 'server'
  })

  return normalized
}

const normalizeSortMode = (raw) => {
  if (typeof raw !== 'string') {
    return 'default'
  }
  return SORT_MODES.includes(raw) ? raw : 'default'
}

const parsePriceInfo = (rawPrice) => {
  if (typeof rawPrice !== 'string' || !rawPrice.trim()) {
    return null
  }

  const digits = rawPrice.replace(/\D/g, '')
  if (!digits) {
    return null
  }

  const currency = rawPrice.includes('₸') ? '₸' : rawPrice.includes('₽') ? '₽' : null
  return {
    value: Number(digits),
    currency,
  }
}

const buildExtremesByCurrency = (items) => {
  const buckets = {}

  items.forEach((item) => {
    const priceInfo = parsePriceInfo(item?.price)
    if (!priceInfo || !priceInfo.currency || !Number.isFinite(priceInfo.value)) {
      return
    }

    const current = buckets[priceInfo.currency]
    if (!current) {
      buckets[priceInfo.currency] = {
        currency: priceInfo.currency,
        min: { item, value: priceInfo.value },
        max: { item, value: priceInfo.value },
      }
      return
    }

    if (priceInfo.value < current.min.value) {
      current.min = { item, value: priceInfo.value }
    }
    if (priceInfo.value > current.max.value) {
      current.max = { item, value: priceInfo.value }
    }
  })

  return Object.values(buckets).sort((a, b) => a.currency.localeCompare(b.currency))
}

const loadJson = (key) => {
  try {
    const raw = sessionStorage.getItem(key)
    if (!raw) {
      return null
    }

    return JSON.parse(raw)
  } catch {
    return null
  }
}

const readSessionString = (key, fallback = '') => {
  try {
    return sessionStorage.getItem(key) || fallback
  } catch {
    return fallback
  }
}

export default function SearchPage() {
  const restoredQuery = useMemo(() => readSessionString(LAST_SEARCH_QUERY_KEY), [])
  const restoredActiveTab = useMemo(() => {
    const savedTab = readSessionString(LAST_ACTIVE_TAB_KEY, 'kaspi')
    return SOURCES.includes(savedTab) ? savedTab : 'kaspi'
  }, [])

  const [query, setQuery] = useState(restoredQuery)
  const [activeTab, setActiveTab] = useState(restoredActiveTab)
  const [sortMode, setSortMode] = useState('default')
  const [results, setResults] = useState(createEmptyResults())
  const [totalCounts, setTotalCounts] = useState(createEmptyCounts())
  const [sourceMeta, setSourceMeta] = useState(createEmptySourceMeta())
  const [sourceModes, setSourceModes] = useState(createEmptySourceModes())
  const [sourceErrors, setSourceErrors] = useState({})
  const [sourceSuppliers, setSourceSuppliers] = useState({})
  const [globalSuppliers, setGlobalSuppliers] = useState([])
  const [loading, setLoading] = useState(false)
  const [toast, setToast] = useState('')

  const applySearchPayload = (payload) => {
    if (!payload || !Array.isArray(payload.results)) {
      return
    }

    setGlobalSuppliers(payload.suppliers || [])

    const nextResults = createEmptyResults()
    const nextCounts = createEmptyCounts()
    const nextSourceMeta = createEmptySourceMeta()
    const nextSourceModes = createEmptySourceModes()
    const nextErrors = {}
    const nextSuppliers = {}

    payload.results.forEach((entry) => {
      const items = Array.isArray(entry.items) ? entry.items : []
      const source = entry.source
      if (!SOURCES.includes(source)) {
        return
      }

      const rawMeta = entry?.meta && typeof entry.meta === 'object' ? entry.meta : {}
      const metaSellers = Array.isArray(rawMeta.sellers)
        ? rawMeta.sellers.filter((value) => typeof value === 'string' && value.trim())
        : []
      const metaTotal = Number.isFinite(rawMeta.total_found) ? rawMeta.total_found : null
      const metaSellersFound = Number.isFinite(rawMeta.sellers_unique_count)
        ? rawMeta.sellers_unique_count
        : metaSellers.length
      const metaSellersKnownItems = Number.isFinite(rawMeta.sellers_known_items)
        ? rawMeta.sellers_known_items
        : metaSellers.length

      nextResults[source] = items.slice(0, 10)
      nextCounts[source] = metaTotal !== null && metaTotal >= 0 ? metaTotal : items.length
      nextSourceMeta[source] = {
        sellers: metaSellers,
        sellersFound: metaSellersFound,
        sellersKnownItems: metaSellersKnownItems,
      }
      nextSourceModes[source] = entry?.source_mode === 'client' ? 'client' : 'server'
      nextSuppliers[source] = entry.suppliers || []
      if (entry.error) {
        nextErrors[source] = entry.error
      }
    })

    setResults(nextResults)
    setTotalCounts(nextCounts)
    setSourceMeta(nextSourceMeta)
    setSourceModes(nextSourceModes)
    setSourceErrors(nextErrors)
    setSourceSuppliers(nextSuppliers)
  }

  useEffect(() => {
    try {
      sessionStorage.setItem(LAST_ACTIVE_TAB_KEY, activeTab)
    } catch {
      // Ignore storage errors; the page remains usable without persistence.
    }
  }, [activeTab])

  useEffect(() => {
    const savedPayload = loadJson(LAST_SEARCH_PAYLOAD_KEY)
    if (savedPayload) {
      applySearchPayload(savedPayload)
    }
  }, [])

  const handleSearch = async (event) => {
    event.preventDefault()
    const prepared = query.trim()
    if (prepared.length < 2) {
      return
    }

    setLoading(true)
    setToast('')

    try {
      const payload = await api.search(prepared)
      applySearchPayload(payload)
      setQuery(prepared)
      setActiveTab('kaspi')
      try {
        sessionStorage.setItem(LAST_SEARCH_PAYLOAD_KEY, JSON.stringify(payload))
        sessionStorage.setItem(LAST_SEARCH_QUERY_KEY, prepared)
        sessionStorage.setItem(LAST_ACTIVE_TAB_KEY, 'kaspi')
      } catch {
        // Ignore storage errors; the page remains usable without persistence.
      }
    } catch (error) {
      setToast(`Ошибка поиска: ${error.message}`)
    } finally {
      setLoading(false)
    }
  }

  const activeItems = useMemo(() => results[activeTab] || [], [results, activeTab])
  const sortedActiveItems = useMemo(() => {
    if (sortMode === 'default') {
      return activeItems
    }

    const direction = sortMode === 'price_desc' ? -1 : 1
    return [...activeItems].sort((left, right) => {
      const leftPrice = parsePriceInfo(left?.price)
      const rightPrice = parsePriceInfo(right?.price)

      if (!leftPrice && !rightPrice) {
        return 0
      }
      if (!leftPrice) {
        return 1
      }
      if (!rightPrice) {
        return -1
      }

      const leftCurrency = leftPrice.currency || ''
      const rightCurrency = rightPrice.currency || ''
      if (leftCurrency !== rightCurrency) {
        return leftCurrency.localeCompare(rightCurrency)
      }

      return (leftPrice.value - rightPrice.value) * direction
    })
  }, [activeItems, sortMode])

  const activeAnalytics = useMemo(() => {
    const activeMeta = sourceMeta[activeTab] || { sellers: [], sellersFound: 0, sellersKnownItems: 0 }
    const knownSellers = activeItems
      .map((item) => (typeof item?.seller === 'string' ? item.seller.trim() : ''))
      .filter(Boolean)
    const uniqueSellers = Array.from(new Set(knownSellers)).sort((a, b) => a.localeCompare(b, 'ru'))
    const metaSellers = Array.isArray(activeMeta.sellers) ? activeMeta.sellers : []
    const sellersList = metaSellers.length > 0 ? metaSellers : uniqueSellers
    const sellersFound = Number.isFinite(activeMeta.sellersFound) ? activeMeta.sellersFound : sellersList.length
    const sellersKnownItems = Number.isFinite(activeMeta.sellersKnownItems)
      ? activeMeta.sellersKnownItems
      : knownSellers.length

    return {
      totalVariants: totalCounts[activeTab] ?? activeItems.length,
      sellersFound,
      sellersKnownItems,
      sellers: sellersList,
      extremesByCurrency: buildExtremesByCurrency(activeItems),
    }
  }, [activeItems, activeTab, totalCounts, sourceMeta])

  const globalExtremesByCurrency = useMemo(() => {
    const allItems = Object.values(results).flat()
    return buildExtremesByCurrency(allItems)
  }, [results])

  return (
    <main className="page">
      <section className="hero">
        <h1>Smart Catalog</h1>
        <p>Поиск товаров сразу в Kaspi, Wildberries и Ozon</p>
        <SearchBar value={query} onChange={setQuery} onSubmit={handleSearch} loading={loading} />
      </section>

      <ProxyPanel notify={setToast} />

      <section className="results">
        <div className="results-head">
          <div>
            <h2>Результаты</h2>
            <p>Первые 10 товаров по каждому источнику</p>
          </div>
          <div className="sort-control">
            <label htmlFor="price-sort">Сортировка по цене</label>
            <select id="price-sort" value={sortMode} onChange={(event) => setSortMode(event.target.value)}>
              <option value="default">По умолчанию</option>
              <option value="price_asc">Сначала дешевле</option>
              <option value="price_desc">Сначала дороже</option>
            </select>
          </div>
        </div>

        <Tabs
          activeTab={activeTab}
          onTabChange={setActiveTab}
          totalCounts={totalCounts}
          sourceMeta={sourceMeta}
        />

        <div className="analytics-panel">
          <div className="analytics-row">
            <div className="analytics-stat">
              <span>Вариантов найдено</span>
              <strong>{activeAnalytics.totalVariants}</strong>
            </div>
            <div className="analytics-stat">
              <span>Источник данных</span>
              <strong>{sourceModes[activeTab] === 'client' ? 'Браузер' : 'Сервер'}</strong>
            </div>
            <div className="analytics-stat">
              <span>Уникальных продавцов</span>
              <strong>{activeAnalytics.sellersFound}</strong>
            </div>
            <div className="analytics-stat">
              <span>Карточек с продавцом</span>
              <strong>{activeAnalytics.sellersKnownItems}</strong>
            </div>
          </div>

          {activeAnalytics.sellers.length > 0 ? (
            <div className="sellers-block">
              <span className="sellers-label">
                Продавцы ({activeAnalytics.sellers.length}):
              </span>
              <div className="sellers-chips">
                {activeAnalytics.sellers.map((seller) => (
                  <span key={seller} className="seller-chip">{seller}</span>
                ))}
              </div>
            </div>
          ) : (
            <p className="analytics-line muted">Продавцы в этой выдаче не определены.</p>
          )}

          <div className="extremes-grid">
            {activeAnalytics.extremesByCurrency.length > 0 ? (
              activeAnalytics.extremesByCurrency.map((entry) => (
                <article key={`active-${entry.currency}`} className="extreme-card">
                  <h3>Экстремумы во вкладке ({entry.currency})</h3>
                  <p>
                    <strong>Самый дешевый:</strong> {entry.min.item.title} - {entry.min.item.price}
                  </p>
                  <p>
                    <strong>Самый дорогой:</strong> {entry.max.item.title} - {entry.max.item.price}
                  </p>
                </article>
              ))
            ) : (
              <article className="extreme-card">
                <h3>Экстремумы во вкладке</h3>
                <p>Нет достаточных данных по ценам для расчета.</p>
              </article>
            )}

            {globalExtremesByCurrency.length > 0 &&
              globalExtremesByCurrency.map((entry) => (
                <article key={`global-${entry.currency}`} className="extreme-card global">
                  <h3>Глобально по всем вкладкам ({entry.currency})</h3>
                  <p>
                    <strong>Самый дешевый:</strong>{' '}
                    <a href={entry.min.item.product_url} target="_blank" rel="noreferrer">
                      {entry.min.item.title}
                    </a>{' '}
                    - {entry.min.item.price}
                  </p>
                  <p>
                    <strong>Самый дорогой:</strong>{' '}
                    <a href={entry.max.item.product_url} target="_blank" rel="noreferrer">
                      {entry.max.item.title}
                    </a>{' '}
                    - {entry.max.item.price}
                  </p>
                </article>
              ))}
          </div>
        </div>

        {sourceSuppliers[activeTab]?.length > 0 && (
          <div className="suppliers-panel">
            <div className="section-heading">
              <div>
                <p className="eyebrow">Suppliers</p>
                <h3>Поставщики</h3>
              </div>
              <span>{sourceSuppliers[activeTab].length} найдено</span>
            </div>

            <div className="suppliers-list">
              {sourceSuppliers[activeTab].slice(0, 10).map((supplier) => (
                <div className="supplier-card" key={`${activeTab}-${supplier.name}`}>
                  <div>
                    <strong>{supplier.name}</strong>
                    <span>{supplier.products_count} товаров</span>
                  </div>

                  <div className="supplier-prices">
                    {supplier.min_price && supplier.max_price ? (
                      <>
                        <span>
                          от {supplier.min_price.toLocaleString('ru')} ₸
                        </span>
                        <span>
                          до {supplier.max_price.toLocaleString('ru')} ₸
                        </span>
                      </>
                    ) : (
                      <span>Цена не указана</span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {sourceErrors[activeTab] && <p className="source-error">Источник вернул ошибку: {sourceErrors[activeTab]}</p>}

        {loading && <Loader text="Собираем данные с маркетплейсов..." />}

        {!loading && activeItems.length === 0 && (
          <p className="empty">Ничего не найдено в этой вкладке. Попробуйте другой запрос.</p>
        )}

        <div className="product-grid">
          {!loading &&
            sortedActiveItems.map((item) => (
              <ProductCard
                key={`${item.source}-${item.product_url}`}
                item={item}
              />
            ))}
        </div>
      </section>

      <Toast message={toast} onClose={() => setToast('')} />
    </main>
  )
}
