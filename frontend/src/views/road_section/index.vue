<template>
  <section class="page" data-module="road_section">
    <header class="page-head">
      <div>
        <h2>路段管理管理</h2>
        <p class="page-desc">维护管养路段，围绕路段编号、路段名称、起止桩号、道路等级做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记管养路段</button>
        <button class="btn" type="button" @click="exportRows">导出路段管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="map-panel">
      <h3 class="map-title">地图区间（以最新审定公告为准）</h3>
      <ul class="map-list">
        <li v-for="item in mapIntervals" :key="String(item.id)">
          <strong>{{ item['路段名称'] }}</strong>
          <span>{{ item['起止桩号'] }}</span>
          <span class="map-meta">{{ item['状态'] }} · 第 {{ item['版本号'] }} 版</span>
        </li>
        <li v-if="!mapIntervals.length" class="map-meta">暂无区间数据</li>
      </ul>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <form v-if="editing" class="edit-panel" @submit.prevent="saveEdit">
      <h3 class="map-title">
        编辑管养路段 {{ editing['路段编号'] }}（基于第 {{ editing.base_version }} 版）
      </h3>
      <div class="edit-grid">
        <label v-for="field in editableFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="editing.values[field]" :placeholder="`请输入${field}`" />
        </label>
      </div>
      <div class="edit-actions">
        <button class="btn primary" type="submit" :disabled="saving">
          {{ saving ? '保存中…' : '保存路段主数据' }}
        </button>
        <button class="btn ghost" type="button" @click="cancelEdit">取消</button>
        <span class="edit-hint">保存后巡查台账、病害空间索引、工程待办与车辆任务同步新边界</span>
      </div>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openEdit(row)">编辑</button>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无路段管理数据，可先登记管养路段</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条路段管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface EditState {
  id: number
  路段编号: string
  base_version: number
  request_id: string
  values: Record<string, string>
}

const ENDPOINT = '/api/road_section'
const columns = ["路段编号", "路段名称", "起止桩号", "道路等级", "车道数", "路面类型", "管养单位", "路段状态", "版本号"]
const editableFields = ["路段名称", "起止桩号", "道路等级", "车道数", "路面类型", "管养单位"]
const actions = ["设置施工", "设置限行", "恢复通行"]
const statuses = ["正常", "施工", "限行", "封闭"]
const stats = [{"label": "正常路段", "value": 0}, {"label": "施工路段", "value": 0}, {"label": "限行路段", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const mapIntervals = ref<Row[]>([])
const editing = ref<EditState | null>(null)
const saving = ref(false)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '管养路段登记入口尚未接入审批流'
}

function newRequestId() {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID()
  }
  return `req-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function openEdit(row: Row) {
  errorMessage.value = ''
  const values: Record<string, string> = {}
  for (const field of editableFields) {
    values[field] = String(row[field] ?? '')
  }
  editing.value = {
    id: Number(row.id),
    路段编号: String(row['路段编号'] ?? ''),
    base_version: Number(row['version'] ?? row['版本号'] ?? 1),
    // 一次编辑只生成一个请求标识：重复点击/重试都幂等，不会产生两份路段版本
    request_id: newRequestId(),
    values,
  }
}

function cancelEdit() {
  editing.value = null
}

async function saveEdit() {
  if (!editing.value || saving.value) {
    return
  }
  saving.value = true
  errorMessage.value = ''
  const current = editing.value
  try {
    const response = await request(`${ENDPOINT}/${current.id}`, {
      method: 'PUT',
      body: JSON.stringify({
        values: current.values,
        base_version: current.base_version,
        request_id: current.request_id,
      }),
    })
    const payload = await response.json().catch(() => ({}))
    if (response.status === 409) {
      // 版本锁冲突：他人已审定新版本，刷新列表拿到最新边界后再改
      errorMessage.value = String(payload.detail ?? '版本锁冲突，请刷新后重试')
      editing.value = null
      await reload()
      return
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(String(payload.detail ?? payload.message ?? '路段主数据保存未生效'))
    }
    editing.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路段管理操作失败'
  } finally {
    saving.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('路段管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路段管理操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResponse, mapResponse] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/map_intervals`),
    ])
    if (!listResponse.ok) {
      throw new Error('管养路段列表读取失败')
    }
    const payload = await listResponse.json()
    rows.value = (payload.items ?? []).map((item: Row) => ({
      ...item,
      版本号: item['version'] ?? item['版本号'] ?? 1,
    }))
    total.value = payload.total ?? rows.value.length
    if (mapResponse.ok) {
      const mapPayload = await mapResponse.json()
      mapIntervals.value = mapPayload.items ?? []
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路段管理列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.map-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.map-title {
  font-size: 13px;
  margin: 0 0 8px;
}
.map-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 13px;
}
.map-list li {
  display: flex;
  gap: 8px;
  align-items: baseline;
}
.map-meta {
  color: var(--muted);
  font-size: 12px;
}
.edit-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.edit-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}
.edit-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}
.edit-hint {
  color: var(--muted);
  font-size: 12px;
}
</style>
