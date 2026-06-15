const KASPI_BASE_URL = 'https://kaspi.kz'

const cleanText = (value) => {
  if (typeof value !== 'string') {
    return ''
  }
  return value
    .replace(/\u00a0|\u202f/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

const looksLikeTitle = (value) => {
  const title = cleanText(value)
  if (!title || title.length < 4) {
    return false
  }
  return /[A-Za-zА-Яа-я0-9]/.test(title)
}

const normalizeLink = (href) => {
  if (!href || typeof href !== 'string') {
    return null
  }

  try {
    return new URL(href, KASPI_BASE_URL).toString()
  } catch {
    return null
  }
}

const normalizeProductUrl = (url) => {
  if (!url) {
    return null
  }
  return url
    .replace('&tab=reviews', '')
    .replace('?tab=reviews', '')
}

const extractPrice = (text) => {
  const normalized = cleanText(text)
  if (!normalized) {
    return null
  }

  const hasTenge = normalized.includes('₸') || normalized.toLowerCase().includes('тенге') || normalized.toLowerCase().includes('тг')
  const match = normalized.match(/(\d[\d\s\u00a0]{2,})/)
  if (!match) {
    return null
  }

  const digits = match[1].replace(/\D/g, '')
  if (!digits) {
    return null
  }

  if (!hasTenge && digits.length < 3) {
    return null
  }

  const value = Number(digits)
  if (!Number.isFinite(value)) {
    return null
  }

  return `${value.toLocaleString('ru-RU').replace(/,/g, ' ')} ₸`
}

const extractSeller = (text) => {
  const normalized = cleanText(text)
  if (!normalized) {
    return null
  }

  const match = normalized.match(/(?:продавец|магазин|seller|shop)\s*[:\-]?\s*([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\s."'\-]{1,80})/i)
  if (!match) {
    return null
  }

  const seller = cleanText(match[1]).replace(/[.,:;\-]+$/, '').trim()
  return seller || null
}

const extractRating = (text) => {
  const normalized = cleanText(text)
  if (!normalized) {
    return null
  }

  const match = normalized.match(/(?<![\d.,])([1-5](?:[.,]\d{1,2})?)(?![\d.,])/)
  if (!match) {
    return null
  }

  const value = Number(match[1].replace(',', '.'))
  if (!Number.isFinite(value) || value < 1 || value > 5) {
    return null
  }

  return `${value}`
}

const extractReviewsCount = (text) => {
  const normalized = cleanText(text)
  if (!normalized) {
    return null
  }

  const match = normalized.match(/(?<![.,\d])(\d+[\d\s\u00a0]*)\s*(?:отзыв(?:а|ов)?|оцен(?:ка|ки|ок)?|review(?:s)?)/i)
  if (!match) {
    return null
  }

  const digits = match[1].replace(/\D/g, '')
  return digits || null
}

const extractTotalFound = (html, text) => {
  const combined = `${cleanText(text)}\n${html || ''}`
  const patterns = [
    /все\s*категори(?:и|я)\s*\(\s*(\d[\d\s\u00a0]{0,12})\s*\)/gi,
    /(?:найден[оы]?|нашл[оаи]?сь|всего|результат(?:ов|а)?)\s*[:\-]?\s*(\d[\d\s\u00a0]{0,12})\s*(?:товар(?:ов|а)?|результат(?:ов|а)?|предложени(?:й|я))/gi,
    /(\d[\d\s\u00a0]{0,12})\s*(?:товар(?:ов|а)?|результат(?:ов|а)?|предложени(?:й|я))/gi,
    /"(?:total|totalCount|totalResults|found)"\s*:\s*(\d{1,12})/gi,
  ]

  const candidates = []
  patterns.forEach((pattern) => {
    let match = pattern.exec(combined)
    while (match) {
      const digits = String(match[1] || '').replace(/\D/g, '')
      if (digits) {
        const value = Number(digits)
        if (Number.isFinite(value) && value >= 0 && value <= 50_000_000) {
          candidates.push(value)
        }
      }
      match = pattern.exec(combined)
    }
  })

  if (candidates.length === 0) {
    return null
  }

  return Math.max(...candidates)
}

const parseKaspiCards = (html, limit = 10) => {
  const parser = new DOMParser()
  const doc = parser.parseFromString(html, 'text/html')
  const links = doc.querySelectorAll('a[href*="/shop/p/"]')
  const items = []
  const seen = new Set()

  links.forEach((link) => {
    if (items.length >= limit) {
      return
    }

    const productUrl = normalizeProductUrl(normalizeLink(link.getAttribute('href')))
    if (!productUrl || seen.has(productUrl)) {
      return
    }

    const container = link.closest('article, li, div') || link
    const containerText = cleanText(container?.textContent || '')

    const titleCandidates = [
      link.getAttribute('aria-label'),
      link.getAttribute('title'),
      container?.querySelector('[data-testid*=title], [class*=title], [class*=name], h2, h3')?.textContent,
      link.textContent,
    ]

    const title = titleCandidates.map(cleanText).find(looksLikeTitle)
    if (!title) {
      return
    }

    const price = extractPrice(containerText)
    if (!price) {
      return
    }

    const imageUrl =
      normalizeLink(container?.querySelector('img[src]')?.getAttribute('src')) ||
      normalizeLink(container?.querySelector('img[data-src]')?.getAttribute('data-src')) ||
      null

    items.push({
      source: 'kaspi',
      title,
      image_url: imageUrl,
      price,
      seller: extractSeller(containerText),
      product_url: productUrl,
      rating: extractRating(containerText),
      reviews_count: extractReviewsCount(containerText),
    })
    seen.add(productUrl)
  })

  const knownSellers = items
    .map((item) => (typeof item.seller === 'string' ? item.seller.trim() : ''))
    .filter(Boolean)
  const uniqueSellers = Array.from(new Set(knownSellers)).sort((a, b) => a.localeCompare(b, 'ru'))

  const totalFound = extractTotalFound(html, doc.body?.textContent || '')

  return {
    source: 'kaspi',
    items,
    meta: {
      total_found: totalFound !== null ? totalFound : items.length,
      sellers: uniqueSellers,
      sellers_unique_count: uniqueSellers.length,
      sellers_known_items: knownSellers.length,
    },
    error: null,
    source_mode: 'client',
  }
}

const asDirectSearchError = (error) => {
  const message = error instanceof Error ? error.message : String(error)
  const lower = message.toLowerCase()
  if (lower.includes('failed to fetch') || lower.includes('networkerror')) {
    return { code: 'network_or_cors', message }
  }
  if (lower.includes('timeout')) {
    return { code: 'timeout', message }
  }
  return { code: 'unknown', message }
}

export async function searchKaspiDirect(query, { limit = 10, timeoutMs = 8000 } = {}) {
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const url = `${KASPI_BASE_URL}/shop/search/?text=${encodeURIComponent(query)}`
    const response = await fetch(url, {
      method: 'GET',
      signal: controller.signal,
      mode: 'cors',
      credentials: 'omit',
      headers: {
        Accept: 'text/html,application/xhtml+xml',
      },
    })

    if (!response.ok) {
      throw new Error(`Kaspi direct request failed with status ${response.status}`)
    }

    const html = await response.text()
    const parsed = parseKaspiCards(html, limit)
    if (!Array.isArray(parsed.items) || parsed.items.length === 0) {
      throw new Error('Kaspi direct parser returned no products')
    }

    return parsed
  } catch (error) {
    const normalized = asDirectSearchError(error)
    throw new Error(`${normalized.code}: ${normalized.message}`)
  } finally {
    clearTimeout(timeoutId)
  }
}
