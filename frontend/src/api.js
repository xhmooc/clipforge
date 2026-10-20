const BASE = '/api'

async function request(path, options) {
  const response = await fetch(BASE + path, options)
  if (!response.ok) {
    throw new Error((await response.text()) || `请求失败: ${response.status}`)
  }
  return response.json()
}

const json = (method, payload) => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload),
})

const multipart = (file) => {
  const form = new FormData()
  form.append('file', file)
  return { method: 'POST', body: form }
}

export const api = {
  getSettings: () => request('/settings'),
  saveSettings: (settings) => request('/settings', json('PUT', settings)),
  listProjects: () => request('/projects'),
  getProject: (id) => request(`/projects/${id}`),
  upload: (file) => request('/projects', multipart(file)),
  transcribe: (id) => request(`/projects/${id}/transcribe`, { method: 'POST' }),
  saveSubtitles: (id, subtitles) => request(`/projects/${id}/subtitles`, json('PATCH', { subtitles })),
  saveClips: (id, clips) => request(`/projects/${id}/clips`, json('PATCH', { clips })),
  saveStyle: (id, style) => request(`/projects/${id}/style`, json('PATCH', style)),
  analyze: (id, limit) => request(`/projects/${id}/analyze`, json('POST', { limit })),
  render: (id) => request(`/projects/${id}/render`, { method: 'POST' }),
  revealExport: (id) => request(`/projects/${id}/reveal`, { method: 'POST' }),
  uploadSticker: (id, file) => request(`/projects/${id}/stickers`, multipart(file)),
  saveStickers: (id, stickers) => request(`/projects/${id}/stickers`, json('PATCH', { stickers })),
  deleteSticker: (id, name) =>
    request(`/projects/${id}/stickers/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  uploadFont: (id, file) => request(`/projects/${id}/fonts`, multipart(file)),
  buildPreview: (id) => request(`/projects/${id}/preview`, { method: 'POST' }),
  videoUrl: (id) => `${BASE}/projects/${id}/video`,
  concatUrl: (id) => `${BASE}/projects/${id}/concat`,
  stickerUrl: (id, name) => `${BASE}/projects/${id}/stickers/${encodeURIComponent(name)}`,
  renderUrl: (id, name) => `${BASE}/projects/${id}/renders/${encodeURIComponent(name)}`,
}
