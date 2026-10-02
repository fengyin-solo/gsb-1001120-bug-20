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

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
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

    <div v-if="editing" class="dialog-mask" @click.self="closeDialog">
      <form class="dialog" @submit.prevent="saveEdit">
        <h3>{{ dialogTitle }}</h3>
        <label v-for="field in dialogFields" :key="field.key" class="dialog-item">
          <span>{{ field.label }}</span>
          <input v-model="form[field.key]" :placeholder="field.placeholder" />
        </label>
        <p class="dialog-hint">保存后巡查台账、病害空间索引、工程待办与车辆任务将同步新边界；起止桩号格式如 K0+000-K12+500。</p>
        <p v-if="dialogError" class="error-text">{{ dialogError }}</p>
        <div class="dialog-actions">
          <button class="btn primary" type="submit">保存</button>
          <button class="btn ghost" type="button" @click="closeDialog">取消</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/road_section'
const columns = ["路段编号", "路段名称", "起止桩号", "道路等级", "车道数", "路面类型", "管养单位", "路段状态", "地图区间", "版本", "审定公告"]
const actions = ["设置施工", "设置限行", "恢复通行"]
const statuses = ["正常", "施工", "限行", "封闭"]
const stats = [{"label": "正常路段", "value": 0}, {"label": "施工路段", "value": 0}, {"label": "限行路段", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const editing = ref<Row | null>(null)
const dialogTitle = ref('')
const dialogError = ref('')
const form = ref<Record<string, string>>({})
const dialogFields = ref<{ key: string; label: string; placeholder: string }[]>([])

const EDIT_FIELDS = [
  { key: '路段名称', label: '路段名称', placeholder: '如：人民东路' },
  { key: '起止桩号', label: '起止桩号', placeholder: '如：K0+000-K12+500' },
  { key: '审定公告', label: '审定公告', placeholder: '现行桩号以最新审定公告为准' },
]
const CREATE_FIELDS = [
  { key: '路段编号', label: '路段编号', placeholder: '如：ROAD-0009' },
  ...EDIT_FIELDS,
]

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  dialogTitle.value = '登记管养路段'
  dialogFields.value = CREATE_FIELDS
  form.value = {}
  dialogError.value = ''
  editing.value = {}
}

function openEdit(row: Row) {
  dialogTitle.value = `修改 ${row.路段名称 ?? ''}（版本 ${row.版本 ?? 1}）`
  dialogFields.value = EDIT_FIELDS
  form.value = {
    路段名称: String(row.路段名称 ?? ''),
    起止桩号: String(row.起止桩号 ?? ''),
    审定公告: String(row.审定公告 ?? ''),
  }
  dialogError.value = ''
  editing.value = row
}

function closeDialog() {
  editing.value = null
  dialogError.value = ''
}

async function extractError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json()
    if (typeof payload?.detail === 'string') return payload.detail
    if (typeof payload?.message === 'string') return payload.message
  } catch {
    /* 响应体不是 JSON 时走兜底文案 */
  }
  return fallback
}

async function saveEdit() {
  if (editing.value === null) return
  dialogError.value = ''
  const isCreate = !editing.value.id
  const url = isCreate ? ENDPOINT : `${ENDPOINT}/${editing.value.id}`
  const body: Record<string, unknown> = { values: { ...form.value } }
  if (!isCreate) {
    // 版本锁：把编辑时读到的版本号带回服务端，过期提交会被拒绝
    body.version = Number(editing.value.版本 ?? 1)
  }
  try {
    const response = await request(url, {
      method: isCreate ? 'POST' : 'PUT',
      body: JSON.stringify(body),
    })
    if (response.status === 409) {
      dialogError.value = await extractError(response, '路段边界已被他人修改，请刷新后重试')
      await reload()
      return
    }
    if (!response.ok) {
      dialogError.value = await extractError(response, '路段管理保存未生效，请检查填写内容')
      return
    }
    const payload = await response.json()
    if (!payload.ok) {
      dialogError.value = payload.message ?? '路段管理保存未生效，请检查填写内容'
      return
    }
    closeDialog()
    await reload()
  } catch (error) {
    dialogError.value = error instanceof Error ? error.message : '路段管理保存失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
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
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('管养路段列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '管养路段列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.dialog-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.dialog {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px 18px;
  width: 380px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.dialog h3 {
  margin: 0;
  font-size: 15px;
}
.dialog-item span {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.dialog-item input {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
}
.dialog-hint {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}
.dialog-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
</style>
