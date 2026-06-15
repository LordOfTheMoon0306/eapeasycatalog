import { Navigate, Route, Routes } from 'react-router-dom'
import ProductDetailsPage from './pages/ProductDetailsPage'
import SearchPage from './pages/SearchPage'

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<SearchPage />} />
      <Route path="/product" element={<ProductDetailsPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
