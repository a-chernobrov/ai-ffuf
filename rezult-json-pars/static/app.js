const state = {
  files: [],
  currentFile: null,
  results: [],
  meta: {},
  viewed: new Set(),
}

const elements = {
  fileSelect: document.getElementById("fileSelect"),
  onlyUnviewed: document.getElementById("onlyUnviewed"),
  prevUnviewed: document.getElementById("prevUnviewed"),
  nextUnviewed: document.getElementById("nextUnviewed"),
  clearViewed: document.getElementById("clearViewed"),
  reloadButton: document.getElementById("reloadButton"),
  urlSearch: document.getElementById("urlSearch"),
  statusInclude: document.getElementById("statusInclude"),
  statusExclude: document.getElementById("statusExclude"),
  minLength: document.getElementById("minLength"),
  maxLength: document.getElementById("maxLength"),
  excludeLength: document.getElementById("excludeLength"),
  minLines: document.getElementById("minLines"),
  maxLines: document.getElementById("maxLines"),
  excludeLines: document.getElementById("excludeLines"),
  minWords: document.getElementById("minWords"),
  maxWords: document.getElementById("maxWords"),
  excludeWords: document.getElementById("excludeWords"),
  metaInfo: document.getElementById("metaInfo"),
  fileCounts: document.getElementById("fileCounts"),
  counts: document.getElementById("counts"),
  resultsBody: document.getElementById("resultsBody"),
}

const storageKey = "ffuf_viewed_files"

function parseTokens(raw) {
  if (!raw) return []
  return raw
    .split(",")
    .map((value) => value.trim())
    .filter((value) => value.length > 0)
}

function parseNumberList(raw) {
  const tokens = parseTokens(raw)
  return tokens
    .map((value) => Number(value))
    .filter((value) => !Number.isNaN(value))
}

function valueNotExcluded(value, excludedValues) {
  if (excludedValues.length === 0) return true
  return !excludedValues.includes(value)
}

function statusMatches(status, tokens) {
  if (tokens.length === 0) return true
  for (const token of tokens) {
    if (token.endsWith("xx") && token.length === 3 && /^\d$/.test(token[0])) {
      const base = Number(token[0]) * 100
      if (status >= base && status <= base + 99) return true
      continue
    }
    if (token.includes("-")) {
      const parts = token.split("-", 2)
      const start = Number(parts[0])
      const end = Number(parts[1])
      if (!Number.isNaN(start) && !Number.isNaN(end) && status >= start && status <= end) {
        return true
      }
      continue
    }
    const exact = Number(token)
    if (!Number.isNaN(exact) && status === exact) return true
  }
  return false
}

function statusNotExcluded(status, tokens) {
  if (tokens.length === 0) return true
  for (const token of tokens) {
    if (token.endsWith("xx") && token.length === 3 && /^\d$/.test(token[0])) {
      const base = Number(token[0]) * 100
      if (status >= base && status <= base + 99) return false
      continue
    }
    if (token.includes("-")) {
      const parts = token.split("-", 2)
      const start = Number(parts[0])
      const end = Number(parts[1])
      if (!Number.isNaN(start) && !Number.isNaN(end) && status >= start && status <= end) {
        return false
      }
      continue
    }
    const exact = Number(token)
    if (!Number.isNaN(exact) && status === exact) return false
  }
  return true
}

function withinRange(value, minValue, maxValue) {
  if (minValue !== null && value < minValue) return false
  if (maxValue !== null && value > maxValue) return false
  return true
}

function numberOrNull(value) {
  if (value === "" || value === null || value === undefined) return null
  const parsed = Number(value)
  return Number.isNaN(parsed) ? null : parsed
}

function buildRow(item) {
  const row = document.createElement("div")
  row.className = "table-row"

  const status = document.createElement("div")
  status.className = "col status"
  status.textContent = item.status ?? ""
  row.appendChild(status)

  const length = document.createElement("div")
  length.className = "col length"
  length.textContent = item.length ?? 0
  row.appendChild(length)

  const words = document.createElement("div")
  words.className = "col words"
  words.textContent = item.words ?? 0
  row.appendChild(words)

  const lines = document.createElement("div")
  lines.className = "col lines"
  lines.textContent = item.lines ?? 0
  row.appendChild(lines)

  const url = document.createElement("div")
  url.className = "col url"
  const link = document.createElement("a")
  link.href = item.url ?? "#"
  link.target = "_blank"
  link.rel = "noreferrer"
  link.textContent = item.url ?? ""
  url.appendChild(link)
  row.appendChild(url)

  return row
}

function applyFilters() {
  const includeTokens = parseTokens(elements.statusInclude.value)
  const excludeTokens = parseTokens(elements.statusExclude.value)
  const urlFragment = elements.urlSearch.value.trim().toLowerCase()
  const minLength = numberOrNull(elements.minLength.value)
  const maxLength = numberOrNull(elements.maxLength.value)
  const excludeLength = parseNumberList(elements.excludeLength.value)
  const minLines = numberOrNull(elements.minLines.value)
  const maxLines = numberOrNull(elements.maxLines.value)
  const excludeLines = parseNumberList(elements.excludeLines.value)
  const minWords = numberOrNull(elements.minWords.value)
  const maxWords = numberOrNull(elements.maxWords.value)
  const excludeWords = parseNumberList(elements.excludeWords.value)

  const filtered = state.results.filter((item) => {
    const status = Number(item.status ?? 0)
    const length = Number(item.length ?? 0)
    const words = Number(item.words ?? 0)
    const lines = Number(item.lines ?? 0)
    const url = String(item.url ?? "")

    if (!statusMatches(status, includeTokens)) return false
    if (!statusNotExcluded(status, excludeTokens)) return false
    if (!withinRange(length, minLength, maxLength)) return false
    if (!valueNotExcluded(length, excludeLength)) return false
    if (!withinRange(lines, minLines, maxLines)) return false
    if (!valueNotExcluded(lines, excludeLines)) return false
    if (!withinRange(words, minWords, maxWords)) return false
    if (!valueNotExcluded(words, excludeWords)) return false
    if (urlFragment && !url.toLowerCase().includes(urlFragment)) return false

    return true
  })

  renderTable(filtered)
  elements.counts.textContent = `Показано ${filtered.length} из ${state.results.length}`
}

function renderTable(items) {
  const fragment = document.createDocumentFragment()
  for (const item of items) {
    fragment.appendChild(buildRow(item))
  }
  elements.resultsBody.innerHTML = ""
  elements.resultsBody.appendChild(fragment)
}

function updateMeta() {
  const parts = []
  if (state.meta?.time) parts.push(`Время: ${state.meta.time}`)
  if (state.meta?.commandline) parts.push(`Команда: ${state.meta.commandline}`)
  elements.metaInfo.textContent = parts.join(" | ")
}

function loadViewed() {
  const raw = localStorage.getItem(storageKey)
  if (!raw) {
    state.viewed = new Set()
    return
  }
  try {
    const data = JSON.parse(raw)
    if (Array.isArray(data)) {
      state.viewed = new Set(data)
      return
    }
  } catch (error) {
    state.viewed = new Set()
  }
}

function saveViewed() {
  localStorage.setItem(storageKey, JSON.stringify(Array.from(state.viewed)))
}

function markViewed(fileName) {
  if (!fileName) return
  if (!state.viewed.has(fileName)) {
    state.viewed.add(fileName)
    saveViewed()
  }
}

function getFilteredFiles() {
  const onlyUnviewed = elements.onlyUnviewed.checked
  return state.files.filter((file) => {
    if (onlyUnviewed && state.viewed.has(file)) return false
    return true
  })
}

function renderFileOptions() {
  const filtered = getFilteredFiles()
  elements.fileSelect.innerHTML = ""
  for (const file of filtered) {
    const option = document.createElement("option")
    option.value = file
    option.textContent = state.viewed.has(file) ? `${file} ✓` : file
    elements.fileSelect.appendChild(option)
  }
  if (state.currentFile && filtered.includes(state.currentFile)) {
    elements.fileSelect.value = state.currentFile
  }
  const viewedCount = state.viewed.size
  elements.fileCounts.textContent = `Файлы: ${filtered.length} из ${state.files.length} | Просмотрено: ${viewedCount}`
  return filtered
}

async function loadFiles() {
  const response = await fetch("/api/files")
  const data = await response.json()
  state.files = data.files || []
  const filtered = renderFileOptions()
  if (filtered.length > 0) {
    const target = state.currentFile && filtered.includes(state.currentFile) ? state.currentFile : filtered[0]
    elements.fileSelect.value = target
    await loadFile(target)
  } else {
    state.results = []
    renderTable([])
    elements.counts.textContent = "Файлы не найдены"
  }
}

async function loadFile(fileName) {
  if (!fileName) return
  const response = await fetch(`/api/file/${encodeURIComponent(fileName)}`)
  if (!response.ok) {
    state.results = []
    renderTable([])
    elements.counts.textContent = "Файл не найден"
    return
  }
  const data = await response.json()
  state.currentFile = data.file
  state.results = Array.isArray(data.results) ? data.results : []
  state.meta = data.meta || {}
  markViewed(state.currentFile)
  renderFileOptions()
  updateMeta()
  applyFilters()
}

function registerHandlers() {
  elements.reloadButton.addEventListener("click", () => {
    loadFiles()
  })
  elements.fileSelect.addEventListener("change", (event) => {
    loadFile(event.target.value)
  })
  elements.onlyUnviewed.addEventListener("change", () => {
    const filtered = renderFileOptions()
    if (!filtered.includes(state.currentFile)) {
      if (filtered.length > 0) {
        loadFile(filtered[0])
      } else {
        state.results = []
        renderTable([])
        elements.counts.textContent = "Файлы не найдены"
      }
    }
  })
  elements.prevUnviewed.addEventListener("click", () => {
    const filtered = getFilteredFiles()
    const unviewed = filtered.filter((file) => !state.viewed.has(file))
    if (unviewed.length === 0) return
    const currentIndex = unviewed.indexOf(state.currentFile)
    const previous = currentIndex <= 0 ? unviewed[unviewed.length - 1] : unviewed[currentIndex - 1]
    elements.fileSelect.value = previous
    loadFile(previous)
  })
  elements.nextUnviewed.addEventListener("click", () => {
    const filtered = getFilteredFiles()
    const unviewed = filtered.filter((file) => !state.viewed.has(file))
    if (unviewed.length === 0) return
    const currentIndex = unviewed.indexOf(state.currentFile)
    const next = currentIndex === -1 || currentIndex === unviewed.length - 1 ? unviewed[0] : unviewed[currentIndex + 1]
    elements.fileSelect.value = next
    loadFile(next)
  })
  elements.clearViewed.addEventListener("click", () => {
    state.viewed = new Set()
    saveViewed()
    renderFileOptions()
  })
  const inputs = [
    elements.urlSearch,
    elements.statusInclude,
    elements.statusExclude,
    elements.minLength,
    elements.maxLength,
    elements.excludeLength,
    elements.minLines,
    elements.maxLines,
    elements.excludeLines,
    elements.minWords,
    elements.maxWords,
    elements.excludeWords,
  ]
  for (const input of inputs) {
    input.addEventListener("input", () => {
      applyFilters()
    })
  }
}

registerHandlers()
loadViewed()
loadFiles()
