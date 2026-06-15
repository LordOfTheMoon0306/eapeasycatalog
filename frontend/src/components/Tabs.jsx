import { SOURCES } from '../constants/sources'

const LABELS = {
  kaspi: 'Kaspi',
  wildberries: 'Wildberries',
  ozon: 'Ozon',
  satu: 'Satu',
}

export default function Tabs({ activeTab, onTabChange, totalCounts = {}, sourceMeta = {} }) {
  const tabs = SOURCES

  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => {
        const count = totalCounts[tab] ?? 0
        const sellersCount = sourceMeta[tab]?.sellersFound ?? sourceMeta[tab]?.sellers?.length ?? 0
        const isActive = String(activeTab || '').toLowerCase() === tab

        return (
          <button
            key={tab}
            type="button"
            role="tab"
            id={`tab-${tab}`}
            aria-selected={isActive}
            className={`tab-item${isActive ? ' active' : ''}`}
            onClick={() => onTabChange(tab)}
          >
            <span className="tab-label">{LABELS[tab]}</span>
            {count > 0 && (
              <span className="tab-badge tab-badge--items" title={`Найдено товаров: ${count}`}>
                {count.toLocaleString('ru')}
              </span>
            )}
            {sellersCount > 0 && (
              <span className="tab-badge tab-badge--sellers" title={`Продавцов: ${sellersCount}`}>
                {sellersCount} прод.
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
