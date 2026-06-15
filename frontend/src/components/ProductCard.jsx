import { useNavigate } from 'react-router-dom'

const PLACEHOLDER =
  'https://images.unsplash.com/photo-1607082350899-7e105aa886ae?auto=format&fit=crop&w=900&q=70'

export default function ProductCard({ item }) {
  const navigate = useNavigate()

  const openDetails = () => {
    navigate(`/product?source=${encodeURIComponent(item.source)}&url=${encodeURIComponent(item.product_url)}`)
  }

  return (
    <article className="product-card" onClick={openDetails}>
      <div className="product-image-wrap">
        <img src={item.image_url || PLACEHOLDER} alt={item.title} className="product-image" loading="lazy" />
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
              window.open(item.product_url, '_blank', 'noopener,noreferrer')
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
