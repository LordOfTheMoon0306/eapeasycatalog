export default function Loader({ text = 'Загрузка данных...' }) {
  return (
    <div className="loader-wrap">
      <div className="loader" />
      <p>{text}</p>
    </div>
  )
}
