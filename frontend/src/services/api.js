import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000',
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('auth_token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const message =
      error.response?.data?.detail ||
      error.response?.data?.message ||
      error.message ||
      'An unexpected error occurred'
    error.userMessage = message
    return Promise.reject(error)
  }
)

export const healthService = {
  get: () => api.get('/health'),
}

export const analyticsService = {
  getSummary: () => api.get('/api/analytics/summary'),
}

// ─── Transaction Resolution Service (Core MVP) ────────────────────────────────
export const transactionService = {
  getAll: (params) => api.get('/api/transactions', { params }),
  getById: (id) => api.get(`/api/transactions/${id}`),
  create: (data) => api.post('/api/transactions', data),
  getResolution: (id) => api.get(`/api/transactions/${id}/resolution`),
  getTimeline: (id) => api.get(`/api/transactions/${id}/timeline`),
  getDuplicates: (id) => api.get(`/api/transactions/${id}/duplicates`),
  explain: (id) => api.post(`/api/transactions/${id}/explain`),
  generateSupportSummary: (id, data) => api.post(`/api/transactions/${id}/support-summary`, data),
  refund: (id, data = {}) => api.post(`/api/transactions/${id}/refund`, data),
  getDisputes: (id) => api.get(`/api/transactions/${id}/disputes`),
  lookupPayPal: (id) => api.get(`/api/paypal/transactions/${id}`),
}

// ─── Dispute Service (Secondary) ──────────────────────────────────────────────
export const disputeService = {
  getAll: (params) => api.get('/api/disputes', { params }),
  getById: (id) => api.get(`/api/disputes/${id}`),
  create: (data) => api.post('/api/disputes', data),
  updateStatus: (id, status) => api.put(`/api/disputes/${id}/status`, { status }),
  submitEvidence: (id, evidence) => api.post(`/api/disputes/${id}/evidence`, evidence),
  escalate: (id, note) => api.post(`/api/disputes/${id}/escalate`, { note }),
  accept: (id) => api.post(`/api/disputes/${id}/accept`),
}

// ─── Case Service (Secondary) ─────────────────────────────────────────────────
export const caseService = {
  getAll: (params) => api.get('/api/cases', { params }),
  getById: (id) => api.get(`/api/cases/${id}`),
  create: (data) => api.post('/api/cases', data),
  update: (id, data) => api.put(`/api/cases/${id}`, data),
  getSummary: (id) => api.get(`/api/cases/${id}/summary`),
}

// ─── AI Service ───────────────────────────────────────────────────────────────
export const aiService = {
  analyze: (data) => api.post('/api/ai/analyze', data),
  generateTemplate: (data) => api.post('/api/ai/template', data),
  assessRisk: (data) => api.post('/api/ai/risk', data),
}

export default api
