import { useNavigate } from 'react-router-dom'

const PLACEHOLDER =
  'https://images.unsplash.com/photo-1607082350899-7e105aa886ae?auto=format&fit=crop&w=900&q=70'

const productStorageKey = (source, productUrl) => `product:${source}:${productUrl}`

export default function ProductCard({ item }) {
  const navigate = useNavigate()
  const source = String(item.source || '').toLowerCase()
  const productUrl = item.product_url || item.url
  const imageUrl = item.image_url || item.image

  const openDetails = () => {
    const product = {
      ...item,
      source,
      product_url: productUrl,
      image_url: imageUrl,
    }

    try {
      sessionStorage.setItem(productStorageKey(source, productUrl), JSON.stringify(product))
    } catch {
      // Details can still load without the session snapshot.
    }

    navigate(`/product?source=${encodeURIComponent(source)}&url=${encodeURIComponent(productUrl)}`, {
      state: { product },
    })
  }

  return (
    <article className="product-card" onClick={openDetails}>
      <div className="product-image-wrap">
        <img src={imageUrl || PLACEHOLDER} alt={item.title} className="product-image" loading="lazy" />
      </div>
      <div className="product-content">
        <h3>{item.title}</h3>
        <div className="product-meta">
          <span className="price">{item.price || 'Цена не найдена'}</span>
          <span className="rating">{item.rating || 'Без рейтинга'}</span>
          <span className="rating">{item.reviews_count ? `${item.reviews_count} отзывов` : 'Без отзывов'}</span>
        </div>
        <div className="card-actions">
          <button
            type="button"
            className="ghost"
            onClick={(event) => {
              event.stopPropagation()
              window.open(productUrl, '_blank', 'noopener,noreferrer')
            }}
          >
            Открыть
          </button>
          <button type="button" className="primary">
            Детали
          </button>
        </div>
      </div>
    </article>
  )
}
