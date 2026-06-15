export default function SearchBar({ value, onChange, onSubmit, loading }) {
  return (
    <form className="search-panel" onSubmit={onSubmit}>
      <input
        type="text"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="Например: принтер Epson, ламинат"
        className="search-input"
      />
      <button type="submit" className="search-button" disabled={loading || value.trim().length < 2}>
        {loading ? 'Поиск...' : 'Найти'}
      </button>
    </form>
  )
}
