'use strict';

const $ = (id) => document.getElementById(id);
const clone = (value) => structuredClone(value);
const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const uid = () => crypto.randomUUID();
const EDITABLE = ['name', 'source_url', 'source_title', 'settings', 'segments', 'highlights', 'edit_ranges', 'speakers'];
const JOB_LABELS = {waveform:'分析波形',scan:'快速找片',transcribe:'日语识别',translate:'中文初译',semantic:'语义选片',export:'导出文件',proxy:'生成播放代理',download:'下载模型',model_download:'下载模型',download_video:'下载视频'};
const PAGE_SIZE = 120;
const DEFAULT_SPEAKERS = [{id:'kanata',name:'天音彼方',color:'#4C88FF'},{id:'lamy',name:'雪花菈米',color:'#94DEFF'},{id:'korone',name:'戌神沁音',color:'#C9994A'},{id:'azki',name:'AZKi',color:'#E65D76'}];
function projectSpeakers() { return state.project?.speakers || DEFAULT_SPEAKERS; }
function cueColors(cue) { return (cue.speaker_ids || []).map(id => projectSpeakers().find(s => s.id === id)?.color).filter(color => /^#[0-9a-f]{6}$/i.test(color || '')); }
function coloredText(text, cue) {
  const colors = cueColors(cue);
  if (!colors.length) return escapeHtml(text);
  if (colors.length === 1) return `<span style="color:${colors[0]}">${escapeHtml(text)}</span>`;
  const gradient = `linear-gradient(to bottom,${colors.map((color,i) => `${color} ${i/colors.length*100}% ${(i+1)/colors.length*100}%`).join(',')})`;
  return [...text].map(char => char === '\n' ? '<br>' : `<span class="chorus-letter" style="background-image:${gradient}">${escapeHtml(char)}</span>`).join('');
}

const FLAG_LABELS = {sentence_merge:'已整理断句 · 待听校',low_confidence:'低置信度',possible_silence:'疑似无语音',repetition:'疑似重复',fast_speech:'语速过快',boundary_review:'分段边界待核',imported:'已导入',translation_review:'译文待听校',transcript_corrected:'已参考另一转写修订'};
const state = {project:null,base:null,projects:[],system:null,jobs:[],jobStates:new Map(),dismissedJobs:new Set(),selectedId:null,activeHighlight:null,clip:{start:0,end:0},undo:[],saveTimer:null,saving:null,polling:false,zoom:0,viewStart:0,drag:null,loading:false,mediaUrl:'',subtitlePage:0,rowMap:new Map(),playingIds:new Set(),youtubeJobId:null,recovery:[]};

function timecode(value, milliseconds = true) {
  const v = Math.max(0, Number(value) || 0);
  const total = Math.round(v * 1000);
  const h = Math.floor(total / 3600000), m = Math.floor(total / 60000) % 60, s = Math.floor(total / 1000) % 60;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}${milliseconds ? '.' + String(total % 1000).padStart(3,'0') : ''}`;
}
function shortTime(value) {
  const t = Math.floor(Math.max(0, Number(value) || 0));
  return t >= 3600 ? timecode(t, false) : `${Math.floor(t/60)}:${String(t%60).padStart(2,'0')}`;
}
function parseTime(value) {
  const text = String(value).trim().replace(',', '.');
  if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d{1,3})?$/.test(text)) throw new Error('时间格式无效，请填写 00:01:23.450 或秒数。');
  const parts = text.split(':').map(Number);
  if (parts.length > 1 && parts.slice(1).some((n) => n >= 60)) throw new Error('分和秒应小于 60。');
  return parts.reduce((sum,n) => sum * 60 + n, 0);
}
function durationLabel(value) { return value >= 3600 ? `${(value/3600).toFixed(1)} 小时` : value >= 60 ? `${Math.round(value/60)} 分钟` : `${Math.round(value)} 秒`; }
function notify(message, error = false) {
  const node = document.createElement('div'); node.className = `toast${error ? ' error' : ''}`;
  node.textContent = message;
  const close = document.createElement('button'); close.textContent = '×'; close.ariaLabel = '关闭提示'; close.onclick = () => node.remove(); node.append(close);
  $('toast-container').append(node); setTimeout(() => node.remove(), error ? 14000 : 5000);
}
async function api(path, options = {}) {
  const {body, ...rest} = options;
  const response = await fetch(path, {...rest, headers:body && !(body instanceof FormData) ? {'Content-Type':'application/json', ...rest.headers} : rest.headers, body:body instanceof FormData ? body : body === undefined ? undefined : JSON.stringify(body)});
  const content = response.headers.get('content-type') || '';
  const data = content.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = data?.detail || data?.error || data;
    const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail)); error.status = response.status; throw error;
  }
  return data;
}
function handle(action) { return async (event) => { try { await action(event); } catch (error) { notify(error.message || '操作失败，请稍后重试。', true); } }; }
function showDialog(id) { const dialog = $(id); if (!dialog.open) dialog.showModal(); }
function closeDialog(id) { $(id).close(); }
function requireProject() { if (!state.project) throw new Error('请先导入一个视频。'); return state.project; }
function validRange(start = state.clip.start, end = state.clip.end) {
  const p = requireProject();
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || end > p.duration + 0.02) throw new Error('请设置有效的入点与出点，出点需晚于入点且不超过视频时长。');
  return {start:Math.max(0,start),end:Math.min(end,p.duration)};
}

// A field-level three-way merge preserves edits made during saves or processing jobs.
function mergeRecords(remote = [], base = [], local = [], onDeleted = null) {
  const baseMap = new Map(base.map((r) => [r.id,r]));
  const localMap = new Map(local.map((r) => [r.id,r]));
  const remoteMap = new Map(remote.map((r) => [r.id,clone(r)]));
  for (const old of base) if (!localMap.has(old.id)) remoteMap.delete(old.id);
  for (const item of local) {
    const old = baseMap.get(item.id);
    if (!old) { remoteMap.set(item.id,clone(item)); continue; }
    if (equal(item,old)) continue;
    if (!remoteMap.has(item.id)) { if (onDeleted) onDeleted(item); continue; }
    const target = remoteMap.get(item.id);
    for (const key of new Set([...Object.keys(old),...Object.keys(item)])) if (!equal(item[key],old[key])) target[key] = clone(item[key]);
    remoteMap.set(item.id,target);
  }
  return [...remoteMap.values()].sort((a,b) => a.start-b.start || a.end-b.end);
}
function mergeProject(remote, base, local) {
  const merged = clone(remote);
  for (const key of EDITABLE) {
    if (equal(local?.[key],base?.[key])) continue;
    if (key === 'segments' || key === 'highlights') merged[key] = mergeRecords(remote[key],base[key],local[key],key === 'segments' ? (item) => recoverDeletedEdit(remote.id,item) : null);
    else if (key === 'settings') {
      merged.settings = {...remote.settings};
      for (const setting of Object.keys(local.settings || {})) if (!equal(local.settings[setting],base.settings?.[setting])) merged.settings[setting] = clone(local.settings[setting]);
    } else merged[key] = clone(local[key]);
  }
  return merged;
}
function persistRecovery() { try { localStorage.setItem('kotori.editor-recovery.v1',JSON.stringify(state.recovery)); } catch (_) { /* The visible recovery stays available if local storage is full. */ } }
function recoverDeletedEdit(projectId,segment) {
  if (state.recovery.some((r) => r.projectId === projectId && equal(r.segment,segment))) return;
  state.recovery.push({id:uid(),projectId,segment:clone(segment),createdAt:Date.now()}); persistRecovery();
  notify('后台更新替换了一条正在编辑的字幕。你的文字已保留在「待恢复的本地编辑」中。',true);
}
function renderRecovery() {
  let panel = $('subtitle-recovery');
  if (!panel) { panel = document.createElement('div'); panel.id = 'subtitle-recovery'; panel.className = 'subtitle-recovery'; document.querySelector('.subtitle-toolbar').after(panel); }
  const entries = state.recovery.filter((r) => r.projectId === state.project?.id);
  panel.classList.toggle('hidden',!entries.length);
  panel.innerHTML = entries.length ? `<div class="recovery-heading"><strong>待恢复的本地编辑 · ${entries.length}</strong><span>原字幕已被后台结果替换。文字备份仍保留，恢复后请复核重叠时间。</span></div>${entries.map((entry) => `<div class="recovery-item"><div><small>${timecode(entry.segment.start)} → ${timecode(entry.segment.end)}</small><p>${escapeHtml(entry.segment.ja || '')}</p><p>${escapeHtml(entry.segment.zh || '')}</p></div><div class="recovery-actions"><button class="button secondary tiny" data-recovery="${escapeHtml(entry.id)}" data-recovery-action="restore">恢复为新字幕</button><button class="button quiet tiny" data-recovery="${escapeHtml(entry.id)}" data-recovery-action="download">下载文字备份</button><button class="button quiet tiny" data-recovery="${escapeHtml(entry.id)}" data-recovery-action="dismiss">忽略此备份</button></div></div>`).join('')}` : '';
}
function patchFor(project, base) {
  const patch = {revision:base.revision};
  for (const key of EDITABLE) if (!equal(project[key],base[key])) patch[key] = clone(project[key]);
  return patch;
}
function setSaveStatus(label, pending = false) { $('save-status').textContent = label; $('save-status').classList.toggle('pending',pending); }
function markDirty() {
  setSaveStatus('等待保存',true); clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => flushSave().catch(() => {}),700);
}
async function flushSave() {
  clearTimeout(state.saveTimer);
  if (state.saving) { await state.saving; if (state.project && Object.keys(patchFor(state.project,state.base)).length > 1) return flushSave(); return; }
  if (!state.project || !state.base) return;
  state.saving = (async () => {
    let retries = 0;
    while (state.project && Object.keys(patchFor(state.project,state.base)).length > 1) {
      const snapshot = clone(state.project), base = clone(state.base), projectId = snapshot.id;
      setSaveStatus('保存中…',true);
      try {
        const saved = await api(`/api/projects/${projectId}`,{method:'PATCH',body:patchFor(snapshot,base)});
        if (state.project?.id !== projectId) return;
        state.project = mergeProject(saved,snapshot,state.project); state.base = clone(saved); retries = 0;
        renderSubtitles(); updateProjectHeading();
      } catch (error) {
        if (error.status === 409 && retries++ < 5) {
          const remote = await api(`/api/projects/${projectId}`);
          state.project = mergeProject(remote,base,state.project); state.base = clone(remote); continue;
        }
        setSaveStatus('未保存 · 重试',true); notify(`字幕尚未保存：${error.message}`,true); throw error;
      }
    }
    setSaveStatus('已保存');
  })();
  try { await state.saving; } finally { state.saving = null; }
}
function checkpoint() { if (!state.project) return; const segments = clone(state.project.segments); if (!equal(state.undo.at(-1),segments)) state.undo.push(segments); if (state.undo.length > 40) state.undo.shift(); updateEditButtons(); }
function changedSegments() { state.project.segments.sort((a,b) => a.start-b.start || a.end-b.end); markDirty(); renderSubtitles(); drawTimeline(); renderOverlay(); }

async function loadProjects() { state.projects = await api('/api/projects'); renderProjects(); }
function initialClip(project) { return project.edit_ranges?.length === 1 ? clone(project.edit_ranges[0]) : {start:0,end:project.duration}; }
function renderProjects() {
  $('project-count').textContent = state.projects.length;
  $('projects').innerHTML = state.projects.length ? state.projects.map((p) => `<button class="project-item ${p.id === state.project?.id ? 'active' : ''}" data-project="${escapeHtml(p.id)}" title="${escapeHtml(p.name)}"><svg><use href="#i-folder"/></svg><div><strong>${escapeHtml(p.name)}</strong><small>${durationLabel(p.duration || 0)} · ${p.segment_count ?? p.segments?.length ?? 0} 条字幕</small></div></button>`).join('') : '<p class="aside-empty">从一场直播开始。</p>';
}
async function selectProject(projectOrId) {
  if (state.loading) return; state.loading = true;
  try {
    await flushSave(); stopAssembly(); $('video').pause();
    const p = typeof projectOrId === 'string' ? await api(`/api/projects/${projectOrId}`) : projectOrId;
    p.segments ||= []; p.highlights ||= []; p.settings ||= {}; p.exports ||= [];
    state.project = clone(p); state.base = clone(p); state.selectedId = null; state.activeHighlight = null; state.undo = []; state.viewStart = 0; state.subtitlePage = 0; state.rowMap.clear(); state.playingIds.clear();
    state.clip = initialClip(p);
    $('subtitle-filter').value = p.edit_ranges?.length === 1 ? 'clip' : 'all';
    history.replaceState(null,'',`#${p.id}`);
    $('empty-state').classList.add('hidden'); $('workspace').classList.remove('hidden'); $('subtitle-list').replaceChildren();
    setSaveStatus('已保存'); renderCandidateSettings(true); renderProject();
    if (state.clip.start > 0) {
      const start = state.clip.start, jump = () => { if (state.project?.id === p.id) seek(start); };
      if ($('video').readyState >= 1) jump(); else $('video').addEventListener('loadedmetadata',jump,{once:true});
    }
    if (p.import_notice) notify(p.import_notice);
    await loadProjects();
  } finally { state.loading = false; }
}
function updateProjectHeading() {
  const p = state.project; if (!p) return;
  $('header-project').textContent = p.name; $('project-name').textContent = p.name;
  $('project-detail').textContent = `${durationLabel(p.duration)}${p.width ? ` · ${p.width} × ${p.height}` : ''} · ${p.segments.length} 条字幕`;
  $('video-dimensions').textContent = p.width ? `${p.width} × ${p.height}` : '';
  $('duration-time').textContent = `/ ${timecode(p.duration,false)}`;
  $('demo-badge').classList.toggle('hidden', !(p.demo || p.is_demo || /演示/.test(p.name)));
  document.title = `${p.name} · 烤肉工房`;
}
function renderProject() {
  if ($('story-mode') && state.project) $('story-mode').value = state.project.settings.story_mode || 'story';
  const p = state.project; if (!p) return;
  updateProjectHeading(); $('mode-select').value = p.settings.mode || 'mixed';
  renderCandidateSettings();
  const url = `/api/projects/${p.id}/media${p.proxy_path ? '?proxy=' + encodeURIComponent(p.proxy_path) : ''}`;
  if (state.mediaUrl !== url) {
    const time = $('video').currentTime || 0; state.mediaUrl = url; $('media-error').classList.add('hidden'); $('video').src = url;
    if (time && p.id === state.base?.id && p.proxy_path) $('video').addEventListener('loadedmetadata',() => seek(time),{once:true});
  }
  renderClip(); renderCandidates(); renderSubtitles(); renderExports(); renderJobs(); drawTimeline();
}
async function refreshCurrentProject() {
  if (!state.project || state.loading) return;
  const projectId = state.project.id;
  const remote = await api(`/api/projects/${projectId}`);
  if (projectId !== state.project?.id) return;
  if (state.saving) { await state.saving; return refreshCurrentProject(); }
  if (remote.revision < state.base.revision) return;
  state.project = mergeProject(remote,state.base,state.project); state.base = clone(remote); renderProject();
  if (Object.keys(patchFor(state.project,state.base)).length > 1) markDirty();
}

function setClip(start,end,seekStart = false) {
  const range = validRange(start,end); state.clip = range;
  for (const id of ['clip-start','clip-end']) delete $(id).dataset.dirty;
  renderClip(true); renderSubtitles(); drawTimeline(); if (seekStart) seek(start);
}
function commitClipInputs() { setClip(parseTime($('clip-start').value),parseTime($('clip-end').value)); }
function renderClip(force = false) {
  for (const [id,value] of [['clip-start',state.clip.start],['clip-end',state.clip.end]]) if (force || (document.activeElement !== $(id) && !$(id).dataset.dirty)) $(id).value = timecode(value);
  $('clip-length').textContent = `${(state.clip.end-state.clip.start).toFixed(1)} 秒`;
}
function renderCandidateSettings(force = false) {
  if (!state.project) return;
  for (const [id,key,fallback] of [['clip-min','clip_min',30],['clip-max','clip_max',120]]) {
    const input = $(id);
    if (force || (document.activeElement !== input && !input.dataset.dirty)) input.value = state.project.settings[key] ?? fallback;
    if (force) delete input.dataset.dirty;
  }
}
function commitCandidateSettings() {
  const p = requireProject(), min = Number($('clip-min').value), max = Number($('clip-max').value);
  if (!Number.isFinite(min) || !Number.isFinite(max) || min < 5 || max < min || max > 600) throw new Error('候选时长需在 5 至 600 秒内，最长时长不能小于最短时长。');
  const changed = p.settings.clip_min !== min || p.settings.clip_max !== max;
  p.settings.clip_min = min; p.settings.clip_max = max;
  delete $('clip-min').dataset.dirty; delete $('clip-max').dataset.dirty;
  if (changed) markDirty();
}
function seek(time) { const p = requireProject(); $('video').currentTime = Math.max(0,Math.min(p.duration,time)); ensureView($('video').currentTime); renderPlayback(); }
async function togglePlay() { requireProject(); const video = $('video'); if (video.paused) { if ($('loop-check').checked && (video.currentTime < state.clip.start || video.currentTime >= state.clip.end)) seek(state.clip.start); await video.play(); } else video.pause(); }
function renderPlayback() {
  const video = $('video'); $('play-time').textContent = timecode(video.currentTime); $('play-button').textContent = video.paused ? '▶' : 'Ⅱ'; $('play-button').ariaLabel = video.paused ? '播放' : '暂停';
  renderOverlay(); drawTimeline();
  const activeIds = new Set((state.project?.segments || []).filter((s) => s.start <= video.currentTime && s.end > video.currentTime).map((s) => s.id));
  for (const id of state.playingIds) if (!activeIds.has(id)) state.rowMap.get(id)?.classList.remove('playing');
  for (const id of activeIds) if (!state.playingIds.has(id)) state.rowMap.get(id)?.classList.add('playing');
  state.playingIds = activeIds;
}
function renderOverlay() {
  const time = $('video').currentTime;
  const segments = state.project?.segments.filter((s) => s.start <= time && s.end > time) || [];
  const html = segments.map((s) => `${s.zh ? `<div>${coloredText(s.zh,s)}</div>` : ''}${s.ja ? `<div class="ja">${coloredText(s.ja,s)}</div>` : ''}`).join('');
  if ($('subtitle-overlay').innerHTML !== html) $('subtitle-overlay').innerHTML = html;
}
function renderCandidates() {
  const highlights = state.project?.highlights || [];
  $('candidate-count').textContent = highlights.length;
  $('candidates').innerHTML = highlights.length ? highlights.map((h) => `<article class="candidate-card ${h.id === state.activeHighlight ? 'active' : ''}" data-highlight="${escapeHtml(h.id)}" tabindex="0" role="button" aria-label="预览候选 ${escapeHtml(h.title)}"><div class="candidate-top"><span class="candidate-time">${shortTime(h.start)} — ${shortTime(h.end)}</span><span class="candidate-method">${({audio:'声音线索',transcript:'字幕线索',semantic:'语义推荐',story:'故事编排',replay:'回看热度',manual:'边界已调整'})[h.method] || '候选片段'}</span></div><h3>${escapeHtml(h.title || '待查看片段')}</h3><p>${escapeHtml(h.reason || '播放并结合上下文判断。')}</p><div class="candidate-bottom"><span>${Math.round((h.ranges || [h]).reduce((sum,r) => sum+r.end-r.start,0))} 秒${h.ranges ? ` · ${h.ranges.length} 段` : ''}${h.method !== 'manual' && Number.isFinite(h.score) ? ` · 排序分 ${Number(h.score).toFixed(1)}` : ''}</span><button type="button" data-assemble="${escapeHtml(h.id)}">加入拼接</button><button type="button" data-retain="${escapeHtml(h.id)}">${h.selected ? '✓ 已保存切片' : '＋ 保存为切片'}</button></div></article>`).join('') : '<div class="candidate-empty"><svg><use href="#i-spark"/></svg><strong>发现直播里的亮点</strong><span>点击「快速找片」分析声音线索。<br>已有字幕后，可进一步语义选片。</span></div>';
  let batch = $('batch-export-button');
  if (!batch) { batch = document.createElement('button'); batch.id = 'batch-export-button'; batch.className = 'button secondary full small'; batch.style.marginTop = '10px'; batch.onclick = handle(() => openExport('batch')); document.querySelector('.candidates-footer').append(batch); }
  const count = highlights.filter((h) => h.selected).length;
  batch.classList.toggle('hidden',!count); batch.textContent = `批量导出 ${count} 个切片…`;
}
function chooseHighlight(id) {
  const h = state.project.highlights.find((h) => h.id === id); if (!h) return;
  stopAssembly(); state.activeHighlight = h.id; const first = h.ranges?.[0] || h; setClip(first.start,first.end,true); renderCandidates();
}
function savedClips() { return (state.project?.highlights || []).filter(h => h.selected); }
function saveNamedClip(title, ranges, id = '') {
  title = title.trim();
  if (!title || title.length > 200) throw new Error('请填写 1–200 字的切片名称。');
  if (!ranges.length || ranges.length > 32) throw new Error('每个切片需有 1–32 段。');
  ranges = ranges.map(r => validRange(r.start,r.end));
  const project = requireProject(); project.highlights ||= [];
  const existing = id ? project.highlights.find(h => h.id === id && h.selected) : null;
  if (id && !existing) throw new Error('此切片已变动，请重新打开我的切片。');
  const clip = {id:id || uid(),title,ranges:clone(ranges),start:Math.min(...ranges.map(r=>r.start)),end:Math.max(...ranges.map(r=>r.end)),selected:true,method:'manual',reason:'手动保存的切片；可继续调整拼接与校对字幕。'};
  if (existing) Object.assign(existing,clip); else project.highlights.push(clip);
  return clip;
}
function renderSavedClips() {
  const clips = savedClips();
  $('saved-clips').innerHTML = clips.map(h => `<article class="candidate-card"><h3>${escapeHtml(h.title)}</h3><p>${(h.ranges || [h]).length} 段 · ${(h.ranges || [h]).reduce((n,r)=>n+r.end-r.start,0).toFixed(1)} 秒</p><div class="assembly-actions"><button class="button secondary small" data-edit-clip="${escapeHtml(h.id)}">编辑 / 校对</button><button class="button secondary small" data-export-clip="${escapeHtml(h.id)}">导出此片…</button></div></article>`).join('') || '<p class="muted">在原片设置入点和出点，保存第一个切片；也可把推荐候选加入批量。</p>';
  const target = $('clip-save-target'), previous = target.value;
  target.innerHTML = '<option value="">新建切片（保留已有切片）</option>' + clips.map(h=>`<option value="${escapeHtml(h.id)}">更新：${escapeHtml(h.title)}</option>`).join('');
  target.value = clips.some(h=>h.id===previous) ? previous : '';
  $('clips-export').disabled = !clips.length;
}
function openSavedClips() { requireProject(); commitClipInputs(); renderSavedClips(); showDialog('clips-dialog'); }
async function loadSavedClip(id) {
  await flushSave();
  const h = savedClips().find(h=>h.id===id); if (!h) throw new Error('切片不存在。');
  state.project.edit_ranges = clone(h.ranges || [{start:h.start,end:h.end}]);
  state.activeHighlight = h.id; assemblyChanged();
  const first = state.project.edit_ranges[0]; setClip(first.start,first.end,true);
  $('subtitle-filter').value = 'assembly'; renderSubtitles(); renderCandidates();
  $('clip-save-target').value = h.id; $('clip-title').value = h.title; $('clip-save-source').value = 'assembly';
  await flushSave(); return h;
}
async function batchExport() {
  await flushSave();
  const project = requireProject(), highlights = clone(savedClips());
  if (!highlights.length) throw new Error('请先保存切片或把候选加入批量。');
  const format = $('export-format').value, language = $('export-language').value;
  let queued = 0;
  for (const h of highlights) {
    try {
      const job = await api(`/api/projects/${project.id}/jobs`,{method:'POST',body:{kind:'export',options:{ranges:h.ranges || [{start:h.start,end:h.end}],format,language,output_height:Number($('export-height').value),name:h.title}}});
      state.jobs.unshift(job); state.jobStates.set(job.id,job.status); queued++;
    } catch (error) { throw new Error(`已提交 ${queued} 个切片，后续未提交：${error.message}`); }
  }
  notify(`已提交 ${queued} 个切片，将分别生成文件。`); await pollJobs();
}

function makeSubtitleRow(segment) {
  const row = document.createElement('div'); row.className = 'subtitle-row'; row.dataset.id = segment.id;
  row.innerHTML = '<div class="subtitle-meta"><div class="subtitle-index-line"><button class="subtitle-index" data-row-action="seek" title="定位到这条字幕"></button><span class="segment-flags"></span></div><div class="subtitle-times"><label><button class="cue-time-pin" data-row-action="pin-start" title="将字幕入点设为播放头（Alt / ⌥ + I）" aria-label="将字幕入点设为播放头">IN</button><input data-field="start" spellcheck="false" aria-label="字幕开始时间" title="字幕入点；Alt / ⌥ + I 可设为播放头"></label><label><button class="cue-time-pin" data-row-action="pin-end" title="将字幕出点设为播放头（Alt / ⌥ + O）" aria-label="将字幕出点设为播放头">OUT</button><input data-field="end" spellcheck="false" aria-label="字幕结束时间" title="字幕出点；Alt / ⌥ + O 可设为播放头"></label></div><div class="cue-speakers" role="group" aria-label="说话人，可多选齐声"></div></div><textarea data-field="ja" spellcheck="false" aria-label="日文原文" placeholder="日文原文"></textarea><textarea data-field="zh" spellcheck="false" aria-label="中文译文" placeholder="等待初译，或在这里开始翻译"></textarea><button class="review-toggle" data-row-action="review" title="标记为已校对" aria-label="标记为已校对"><svg><use href="#i-check"/></svg></button>';
  return row;
}
function filteredSegments() {
  const filter = $('subtitle-filter').value;
  return (state.project?.segments || []).filter((s) => filter === 'assembly' ? (state.project.edit_ranges || []).some(r => s.end > r.start && s.start < r.end) : filter === 'clip' ? s.end > state.clip.start && s.start < state.clip.end : filter === 'unreviewed' ? !s.reviewed : true);
}
function renderSubtitles() {
  if (!state.project) return;
  const list = $('subtitle-list'), filtered = filteredSegments();
  state.subtitlePage = Math.max(0,Math.min(Math.ceil(filtered.length/PAGE_SIZE)-1,state.subtitlePage));
  const segments = filtered.slice(state.subtitlePage*PAGE_SIZE,(state.subtitlePage+1)*PAGE_SIZE), keep = new Set(segments.map((s) => s.id));
  const existing = new Map([...list.children].map((r) => [r.dataset.id,r]));
  const numbers = new Map(state.project.segments.map((s,i) => [s.id,i+1]));
  state.rowMap.clear();
  for (const [id,row] of existing) if (!keep.has(id)) row.remove();
  segments.forEach((s,index) => {
    const row = existing.get(s.id) || makeSubtitleRow(s);
    state.rowMap.set(s.id,row);
    // Keep existing focused text nodes intact while autosaving or polling.
    for (const input of row.querySelectorAll('[data-field]')) {
      const value = ['start','end'].includes(input.dataset.field) ? timecode(s[input.dataset.field]) : s[input.dataset.field] || '';
      if (document.activeElement !== input && !input.dataset.dirty && input.value !== value) input.value = value;
    }
    const chooser = row.querySelector('.cue-speakers');
    const speakerMarkup = projectSpeakers().map(person => `<label class="speaker-chip" style="--speaker-color:${/^#[0-9a-f]{6}$/i.test(person.color) ? person.color : '#FFFFFF'}"><input type="checkbox" data-speaker-id="${escapeHtml(person.id)}" aria-label="说话人 ${escapeHtml(person.name)}" ${(s.speaker_ids || []).includes(person.id) ? 'checked' : ''}>${escapeHtml(person.name)}</label>`).join('');
    if (chooser.dataset.markup !== speakerMarkup) { chooser.innerHTML = speakerMarkup; chooser.dataset.markup = speakerMarkup; }
    row.querySelector('.subtitle-index').textContent = String(numbers.get(s.id)).padStart(2,'0');
    const flags = (s.flags || []).map((flag) => FLAG_LABELS[flag] || flag); if (!s.zh) flags.push('待翻译');
    row.querySelector('.segment-flags').textContent = flags.join(' · '); row.querySelector('.segment-flags').title = flags.join(' · ');
    const reviewed = row.querySelector('.review-toggle'); reviewed.classList.toggle('reviewed',Boolean(s.reviewed)); reviewed.ariaPressed = String(Boolean(s.reviewed)); reviewed.title = s.reviewed ? '已校对，点击取消' : '标记为已校对';
    row.classList.toggle('selected',s.id === state.selectedId);
    row.classList.toggle('playing',state.playingIds.has(s.id));
    if (list.children[index] !== row) list.insertBefore(row,list.children[index] || null);
  });
  $('subtitle-empty').classList.toggle('hidden',segments.length > 0);
  $('subtitle-empty').querySelector('p').textContent = state.project.segments.length ? '此筛选下没有字幕' : '还没有字幕';
  $('subtitle-empty').querySelector('span').textContent = state.project.segments.length ? '切换筛选条件，或调整视频选区。' : '运行日语识别，或导入已有的 SRT 字幕。';
  $('subtitle-count').textContent = state.project.segments.length;
  const reviewed = state.project.segments.filter((s) => s.reviewed).length;
  $('review-progress').textContent = state.project.segments.length ? `已校对 ${reviewed} / ${state.project.segments.length}` : '';
  renderRecovery();
  let pager = $('subtitle-pager');
  if (!pager) {
    pager = document.createElement('div'); pager.id = 'subtitle-pager'; pager.className = 'subtitle-pager';
    pager.innerHTML = '<span id="subtitle-page-info"></span><div><button id="subtitle-prev-page" class="button quiet tiny">← 上一页</button><button id="subtitle-next-page" class="button quiet tiny">下一页 →</button></div>';
    list.after(pager);
    $('subtitle-prev-page').onclick = () => { state.subtitlePage--; renderSubtitles(); list.scrollTop = 0; };
    $('subtitle-next-page').onclick = () => { state.subtitlePage++; renderSubtitles(); list.scrollTop = 0; };
  }
  pager.classList.toggle('hidden',filtered.length <= PAGE_SIZE);
  $('subtitle-page-info').textContent = `第 ${state.subtitlePage*PAGE_SIZE+1}–${Math.min(filtered.length,(state.subtitlePage+1)*PAGE_SIZE)} 条 / 共 ${filtered.length} 条`;
  $('subtitle-prev-page').disabled = state.subtitlePage === 0;
  $('subtitle-next-page').disabled = (state.subtitlePage+1)*PAGE_SIZE >= filtered.length;
  updateEditButtons();
}
function updateEditButtons() {
  const selected = state.project?.segments.some((s) => s.id === state.selectedId);
  for (const id of ['split-subtitle','merge-subtitle','delete-subtitle']) $(id).disabled = !selected;
  $('shift-subtitle').disabled = !state.project?.segments.length;
  $('sentence-subtitle').disabled = !state.project?.segments.length;
  $('undo-subtitle').disabled = state.undo.length === 0;
}
function selectSegment(id, seekTo = false, scroll = false) {
  const segment = state.project.segments.find((s) => s.id === id); if (!segment) return;
  state.selectedId = id;
  const filteredIndex = filteredSegments().findIndex((s) => s.id === id);
  if (filteredIndex >= 0 && Math.floor(filteredIndex/PAGE_SIZE) !== state.subtitlePage) { state.subtitlePage = Math.floor(filteredIndex/PAGE_SIZE); renderSubtitles(); }
  for (const row of $('subtitle-list').children) row.classList.toggle('selected',row.dataset.id === id);
  updateEditButtons(); if (seekTo) seek(segment.start); else drawTimeline();
  if (scroll) [...$('subtitle-list').children].find((r) => r.dataset.id === id)?.scrollIntoView({block:'nearest',behavior:'smooth'});
}
function selectedSegment() { const s = requireProject().segments.find((s) => s.id === state.selectedId); if (!s) throw new Error('请先选择一条字幕。'); return s; }
function addSubtitle() {
  const p = requireProject(), start = Math.min($('video').currentTime,Math.max(0,p.duration-0.1));
  const next = p.segments.find((s) => s.start > start + 0.1);
  checkpoint(); const segment = {id:uid(),start,end:Math.min(p.duration,next?.start || p.duration,start+3),ja:'',zh:'',speaker:'',reviewed:false,confidence:0,words:[],flags:[]};
  p.segments.push(segment); state.selectedId = segment.id; changedSegments(); selectSegment(segment.id,false,true);
  [...$('subtitle-list').children].find((r) => r.dataset.id === segment.id)?.querySelector('[data-field=ja]').focus();
}
function splitText(text,ratio) {
  if (!text) return ['','']; const chars = [...text]; if (chars.length === 1) return [text,''];
  let at = Math.max(1,Math.min(chars.length-1,Math.round(chars.length*ratio)));
  for (let distance = 0; distance < Math.min(8,chars.length/4); distance++) {
    if (/\s|[。、！？!?，,.]/.test(chars[at+distance] || '')) { at += distance+1; break; }
    if (at-distance > 1 && /\s|[。、！？!?，,.]/.test(chars[at-distance] || '')) { at -= distance-1; break; }
  }
  return [chars.slice(0,at).join('').trim(),chars.slice(at).join('').trim()];
}
function splitSubtitle() {
  const s = selectedSegment(); if (s.end-s.start < 0.2) throw new Error('这条字幕太短，暂时无法拆分。');
  const time = $('video').currentTime; const at = time > s.start + 0.05 && time < s.end-0.05 ? time : (s.start+s.end)/2;
  checkpoint(); const next = {...clone(s),id:uid(),start:at,reviewed:false}; const ratio = (at-s.start)/(s.end-s.start);
  [s.ja,next.ja] = splitText(s.ja,ratio); [s.zh,next.zh] = splitText(s.zh,ratio);
  next.words = (s.words || []).filter((w) => (w.start ?? w.start_time ?? at) >= at); s.words = (s.words || []).filter((w) => (w.start ?? w.start_time ?? 0) < at);
  s.end = at; s.reviewed = false; state.project.segments.push(next); changedSegments(); notify('已拆分字幕，请复核文字分界。');
}
function mergeSubtitle() {
  const p = requireProject(), s = selectedSegment(), index = p.segments.indexOf(s), next = p.segments[index+1];
  if (!next) throw new Error('已是最后一条字幕。'); if (!equal(s.speaker_ids || [],next.speaker_ids || [])) throw new Error('两条字幕的说话人不同，请先统一选择后再合并。'); checkpoint(); s.end = Math.max(s.end,next.end);
  s.ja = [s.ja,next.ja].filter(Boolean).join(' '); s.zh = [s.zh,next.zh].filter(Boolean).join(' '); s.words = [...(s.words || []),...(next.words || [])]; s.reviewed = false; p.segments.splice(index+1,1); changedSegments();
}
function deleteSubtitle() { const s = selectedSegment(); checkpoint(); state.project.segments = state.project.segments.filter((row) => row.id !== s.id); state.selectedId = null; changedSegments(); }
function undoSubtitle() { if (!state.undo.length) return; state.project.segments = state.undo.pop(); changedSegments(); notify('已撤销上一组字幕修改。'); }

let sentencePreview = null;
async function previewSentences() {
  const p = requireProject(); commitClipInputs(); await flushSave();
  sentencePreview = null; $('sentence-apply').disabled = true;
  const baseline = clone(p.segments), projectId = p.id;
  const options = {max_duration:Number($('sentence-duration').value),max_chars:Number($('sentence-chars').value),max_gap:Number($('sentence-gap').value),...($('sentence-scope').value === 'clip' ? validRange() : {})};
  const result = await api(`/api/projects/${projectId}/sentence-preview`,{method:'POST',body:options});
  if (state.project.id !== projectId || !equal(state.project.segments,baseline) || state.project.revision !== result.revision) throw new Error('字幕已更新，请重新预览。');
  sentencePreview = {projectId,baseline,merges:result.merges};
  const originals = new Map(baseline.map(s => [s.id,s]));
  $('sentence-preview').innerHTML = result.merges.length ? result.merges.map((m,i) => `<label class="sentence-proposal"><input type="checkbox" data-merge="${i}" checked><span><strong>${timecode(m.segment.start)} → ${timecode(m.segment.end)} · ${m.ids.length} 条合为一条</strong><small>${m.ids.map(id => escapeHtml(originals.get(id).ja)).join(' / ')}</small><p>${escapeHtml(m.segment.ja)}</p></span></label>`).join('') : '<p class="muted">没有找到适合保守合并的碎句。可调整上限，或用「合并」手动处理。</p>';
  $('sentence-summary').textContent = `建议整理 ${result.merges.length} 组；取消勾选可保留原断句。已校对字幕、不同说话人、明显短回应会跳过；未标说话人的对话仍需试听。`;
  $('sentence-apply').disabled = !result.merges.length;
}
function applySentenceMerges(segments,merges) {
  const replacements = new Map(merges.map(m => [m.ids[0],m.segment]));
  const removed = new Set(merges.flatMap(m => m.ids.slice(1)));
  return segments.filter(s => !removed.has(s.id)).map(s => clone(replacements.get(s.id) || s));
}
async function applySentences() {
  const p = requireProject(), preview = sentencePreview;
  if (!preview || p.id !== preview.projectId || !equal(p.segments,preview.baseline)) throw new Error('字幕已更新，请重新预览后应用。');
  const merges = [...$('sentence-preview').querySelectorAll('[data-merge]:checked')].map(n => preview.merges[Number(n.dataset.merge)]);
  if (!merges.length) throw new Error('请至少勾选一组合并建议。');
  checkpoint(); p.segments = applySentenceMerges(p.segments,merges); changedSegments();
  await flushSave(); closeDialog('sentence-dialog');
  notify(`已整理 ${merges.length} 组断句，保留现有译文；可撤销。中文只是拼接，建议按新句子复核或重译。`);
  if ($('sentence-retranslate').checked) await startJob('translate',{provider:'local',overwrite:true,segment_ids:merges.map(m => m.ids[0])});
}
function commitSubtitleTime(input) {
  const row = input.closest('.subtitle-row'), s = state.project?.segments.find((s) => s.id === row?.dataset.id), field = input.dataset.field;
  if (!s || !['start','end'].includes(field)) return;
  try { const time = parseTime(input.value); validRange(field === 'start' ? time : s.start,field === 'end' ? time : s.end); if (time !== s[field]) { s[field] = time; s.reviewed = false; changedSegments(); } input.value = timecode(time); }
  catch (error) { input.value = timecode(s[field]); throw error; }
  finally { delete input.dataset.dirty; }
}
function pinSubtitleTime(field) {
  const s = selectedSegment(), time = Math.min(state.project.duration,Math.round($('video').currentTime*1000)/1000);
  validRange(field === 'start' ? time : s.start,field === 'end' ? time : s.end);
  if (time === s[field]) return;
  checkpoint(); s[field] = time; changedSegments();
}
function shiftedTimings(segments,offset,duration) {
  if (!Number.isFinite(offset) || !Number.isFinite(duration)) throw new Error('请输入有效的偏移秒数。');
  const delta = Math.round(offset*1000)/1000;
  if (!delta) throw new Error('请输入非零偏移量，最小为 0.001 秒。');
  if (!segments.length) throw new Error('此范围没有字幕，请调整处理范围。');
  return segments.map((s) => {
    const start = Math.round((s.start+delta)*1000)/1000, end = Math.round((s.end+delta)*1000)/1000;
    if (start < 0 || end > duration || end <= start) throw new Error(`偏移后有字幕超出视频范围（原时间 ${timecode(s.start)} → ${timecode(s.end)}），本次操作未应用。`);
    return {id:s.id,start,end};
  });
}
function shiftSelection() {
  const p = requireProject(), scope = $('shift-scope').value;
  if (scope === 'selected') return [selectedSegment()];
  return scope === 'clip' ? p.segments.filter((s) => s.end > state.clip.start && s.start < state.clip.end) : p.segments;
}
function shiftPlan() {
  const text = $('shift-offset').value.trim(); if (!text) throw new Error('请输入要提前或延后的秒数。');
  const offset = Number(text), timings = shiftedTimings(shiftSelection(),offset,state.project.duration);
  return {offset:Math.round(offset*1000)/1000,timings};
}
function renderShiftPreview() {
  try {
    const {offset,timings} = shiftPlan();
    $('shift-preview').textContent = `将 ${timings.length} 条字幕${offset > 0 ? '延后' : '提前'} ${Math.abs(offset).toFixed(3)} 秒。第一条的新时间：${timecode(timings[0].start)} → ${timecode(timings[0].end)}。`;
    $('shift-preview').classList.remove('invalid'); $('shift-apply').disabled = false;
  } catch (error) { $('shift-preview').textContent = error.message; $('shift-preview').classList.add('invalid'); $('shift-apply').disabled = true; }
}

function ensureView(time) {
  if (!state.zoom || !state.project) return;
  if (time < state.viewStart || time > state.viewStart + state.zoom) state.viewStart = Math.max(0,Math.min(state.project.duration-state.zoom,time-state.zoom*0.2));
}
function timelineBounds() {
  const duration = state.project?.duration || 1; const span = state.zoom ? Math.min(state.zoom,duration) : duration;
  state.viewStart = Math.max(0,Math.min(state.viewStart,Math.max(0,duration-span)));
  return {start:state.zoom ? state.viewStart : 0,end:state.zoom ? Math.min(duration,state.viewStart+span) : duration,span};
}
function drawTimeline() {
  if (!state.project) return;
  const canvas = $('timeline'), width = canvas.clientWidth, height = 112, dpr = window.devicePixelRatio || 1;
  if (!width) return;
  if (canvas.width !== Math.round(width*dpr)) canvas.width = Math.round(width*dpr);
  if (canvas.height !== Math.round(height*dpr)) canvas.height = Math.round(height*dpr);
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,width,height);
  const {start,end,span} = timelineBounds(), x = (t) => (t-start)/span*width;
  ctx.fillStyle = '#14161e'; ctx.fillRect(0,0,width,height);
  ctx.fillStyle = '#f89e8b09'; ctx.fillRect(Math.max(0,x(state.clip.start)),0,Math.min(width,x(state.clip.end))-Math.max(0,x(state.clip.start)),height);
  const ticks = Math.max(2,Math.floor(width/110)); ctx.font = '9px ui-monospace, monospace';
  for (let i=0;i<=ticks;i++) { const xx = i*width/ticks; ctx.strokeStyle='#272b37'; ctx.beginPath(); ctx.moveTo(xx,23); ctx.lineTo(xx,height); ctx.stroke(); ctx.fillStyle='#72788f'; ctx.fillText(shortTime(start+(i/ticks)*span),Math.max(5,Math.min(width-56,xx+5)),14); }
  const wave = state.project.waveform || [], step = state.project.waveform_step || 0.1;
  ctx.fillStyle='#76768f';
  if (wave.length) {
    for (let px=0;px<width;px+=3) {
      const first = Math.max(0,Math.floor((start+px/width*span)/step)), last = Math.min(wave.length,Math.ceil((start+(px+3)/width*span)/step));
      let max = 0; const stride = Math.max(1,Math.floor((last-first)/100)); for (let i=first;i<last;i+=stride) max = Math.max(max,Number(wave[i]) || 0);
      const h = Math.max(1,Math.min(1,max)*43); ctx.fillRect(px,57-h/2,1.5,h);
    }
  } else { ctx.fillStyle='#424757'; ctx.fillRect(0,56,width,1); ctx.fillStyle='#6b7288'; ctx.fillText('分析波形后显示音量线索',Math.max(12,width/2-65),49); }
  for (const s of state.project.segments) {
    if (s.end < start || s.start > end) continue; const selected = s.id === state.selectedId;
    ctx.fillStyle = selected ? '#b09af0' : s.reviewed ? '#729b8d' : '#5a526e';
    const left = Math.max(0,x(s.start)), right = Math.min(width,x(s.end)); ctx.fillRect(left,87,Math.max(1,right-left-1),11);
    if (selected) { ctx.fillStyle='#e3d5ff'; ctx.fillRect(left,83,3,19); ctx.fillRect(right-3,83,3,19); }
  }
  for (const t of [state.clip.start,state.clip.end]) if (t >= start && t <= end) { ctx.strokeStyle='#b78175'; ctx.setLineDash([3,4]); ctx.beginPath(); ctx.moveTo(x(t),23); ctx.lineTo(x(t),height); ctx.stroke(); ctx.setLineDash([]); }
  const playX = x($('video').currentTime); if (playX >= 0 && playX <= width) { ctx.fillStyle='#ffb39e'; ctx.fillRect(playX,20,1,height-20); ctx.beginPath(); ctx.moveTo(playX-4,20); ctx.lineTo(playX+4,20); ctx.lineTo(playX,25); ctx.fill(); }
  $('timeline-window').textContent = `${shortTime(start)} — ${shortTime(end)}`;
}
function timelineTime(event) { const rect = $('timeline').getBoundingClientRect(), {start,span} = timelineBounds(); return Math.max(0,Math.min(state.project.duration,start+Math.max(0,Math.min(rect.width,event.clientX-rect.left))/rect.width*span)); }

async function startJob(kind, options = {}, announce = true) {
  const p = requireProject(); await flushSave();
  const job = await api(`/api/projects/${p.id}/jobs`,{method:'POST',body:{kind,options}});
  state.jobs.unshift(job); state.jobStates.set(job.id,job.status); renderJobs();
  if (announce) notify(`已加入任务：${JOB_LABELS[kind] || kind}`); return job;
}
function renderJobs() {
  const jobs = state.jobs.filter((j) => (!j.project_id || j.project_id === state.project?.id) && !state.dismissedJobs.has(j.id) && (['queued','running'].includes(j.status) || j.status === 'failed')).slice(0,8);
  $('job-panel').classList.toggle('hidden',!jobs.length);
  $('job-panel').innerHTML = jobs.map((j) => `<div class="job-row ${j.status === 'failed' ? 'failed' : ''}"><div class="job-content"><strong>${escapeHtml(JOB_LABELS[j.kind] || j.kind)}${j.status === 'queued' ? ' · 排队中' : j.status === 'failed' ? ' · 失败' : ''}</strong><span title="${escapeHtml(j.error || j.message || '')}">${escapeHtml(j.error || j.message || '')}</span><button data-job="${escapeHtml(j.id)}" data-job-action="${j.status === 'failed' ? 'dismiss' : 'cancel'}">${j.status === 'failed' ? '关闭' : '取消'}</button></div><div class="job-progress" style="width:${Math.max(0,Math.min(100,(Number(j.progress)||0)*100))}%"></div></div>`).join('');
}
async function pollJobs() {
  if (state.polling) return; state.polling = true;
  try {
    const jobs = await api('/api/jobs'); let refresh = false, models = false, imported = null;
    for (const job of jobs) {
      const before = state.jobStates.get(job.id);
      if (before && before !== job.status && ['completed','failed','cancelled'].includes(job.status)) {
        if (job.status === 'completed') { if (job.project_id === state.project?.id) refresh = true; if (!job.project_id) models = true; if (job.kind === 'download_video' && job.result?.project_id) imported = job.result.project_id; notify(`${JOB_LABELS[job.kind] || job.kind}已完成${job.message && job.message !== '处理完成' ? `：${job.message}` : ''}`); }
        else if (job.status === 'failed') notify(`${JOB_LABELS[job.kind] || job.kind}失败：${job.error || job.message || '请查看任务详情'}`,true);
      }
      state.jobStates.set(job.id,job.status);
      if (job.id === state.youtubeJobId) { $('youtube-status').textContent = job.error || job.message || ''; const active = ['queued','running'].includes(job.status); $('youtube-import-button').disabled = active; $('youtube-cancel-button').classList.toggle('hidden',!active); }
    }
    state.jobs = jobs; renderJobs();
    if (refresh) { await refreshCurrentProject(); await loadProjects(); }
    if (imported) { await loadProjects(); await selectProject(imported); closeDialog('import-dialog'); }
    if (models || $('system-dialog').open) await loadSystem();
  } catch (error) { console.warn('任务状态暂时不可用',error.message); }
  finally { state.polling = false; }
}
async function loadSystem() { state.system = await api('/api/system'); renderSystem(); }
function renderSystem() {
  const s = state.system; if (!s) return;
  $('disk-status').textContent = `${Number(s.free_gb || 0).toFixed(0)} GB 可用`;
  $('system-overview').innerHTML = `<div class="system-stat"><small>可用磁盘空间</small><strong>${Number(s.free_gb || 0).toFixed(1)} GB</strong></div><div class="system-stat"><small>工作区占用</small><strong>${Number(s.data_gb || 0).toFixed(2)} GB</strong></div><div class="system-stat"><small>视频处理环境</small><strong>${s.ffmpeg && s.ffprobe ? 'FFmpeg 已就绪' : 'FFmpeg 待安装'}</strong></div>`;
  $('storage-path').textContent = `数据位置：${s.data_dir || 'data/'}\n平台：${s.platform || '本机'}\nASR 运行库：${s.asr_available ? '已就绪' : '未安装'} · 本地翻译运行库：${s.llm_available ? '已就绪' : '未安装'}`;
  $('model-list').innerHTML = (s.models || []).map((m) => {
    const downloading = state.jobs.some((j) => !j.project_id && ['queued','running'].includes(j.status) && (j.options?.model === m.id || j.options?.model_id === m.id || j.model_id === m.id || String(j.message || '').includes(m.id)));
    return `<div class="model-card"><div><h3>${escapeHtml(m.name || m.id)}</h3><p>${escapeHtml(m.size_hint || '')}${m.repo ? ' · ' + escapeHtml(m.repo) : ''}</p><span class="${m.installed ? 'installed' : 'not-installed'}">${m.installed ? '● 已下载，可离线使用' : '○ 尚未下载'}</span></div><button class="button ${m.installed ? 'quiet' : 'secondary'} small" data-model="${escapeHtml(m.id)}" ${m.installed || downloading ? 'disabled' : ''}>${m.installed ? '已就绪' : downloading ? '下载中…' : '下载模型'}</button></div>`;
  }).join('') || '<p class="muted">未获取到模型列表。</p>';
  let downloadStatus = $('model-download-status');
  if (!downloadStatus) { downloadStatus = document.createElement('div'); downloadStatus.id = 'model-download-status'; downloadStatus.className = 'model-download-status'; $('model-list').after(downloadStatus); }
  downloadStatus.innerHTML = state.jobs.filter((j) => !j.project_id && ['download','model_download'].includes(j.kind) && ['queued','running','failed'].includes(j.status) && !state.dismissedJobs.has(j.id)).slice(0,5).map((j) => `<div class="model-job"><span>${escapeHtml(j.model_id || '模型')} · ${escapeHtml(j.error || j.message || j.status)}</span><button class="button quiet tiny" data-download-job="${escapeHtml(j.id)}" data-status="${escapeHtml(j.status)}">${j.status === 'failed' ? '关闭' : '取消'}</button></div>`).join('');
  $('cleanup-button').disabled = !state.project;
}
function renderExports() {
  const files = state.project?.exports || []; $('exports-section').classList.toggle('hidden',!files.length);
  $('exports-list').innerHTML = [...files].reverse().map((file) => `<div class="export-item"><span>${escapeHtml(file.name || file.path?.split('/').pop() || '导出文件')}<small>${escapeHtml(file.kind || '')}</small></span><a href="/api/projects/${encodeURIComponent(state.project.id)}/exports/${encodeURIComponent(file.id)}" download>下载文件 ↓</a></div>`).join('');
}

async function importPath(path, name) {
  $('import-status').textContent = '正在读取视频信息…';
  try { const p = await api('/api/projects',{method:'POST',body:{path,name:name || undefined}}); closeDialog('import-dialog'); await selectProject(p); if (!p.import_notice) notify('视频已导入，波形正在后台分析。'); await pollJobs(); }
  finally { $('import-status').textContent = ''; }
}
function subtractRange(parts, cut) {
  return parts.flatMap(part => {
    if (cut.end <= part.start || cut.start >= part.end) return [part];
    return [...(cut.start > part.start ? [{start:part.start,end:cut.start}] : []), ...(cut.end < part.end ? [{start:cut.end,end:part.end}] : [])];
  });
}
function stopAssembly() { state.preview = null; }
function advanceAssembly() {
  const preview = state.preview, video = $('video');
  if (!preview || (video.paused && !video.ended)) return false;
  const part = preview.ranges[preview.index];
  if (video.currentTime >= part.end - .025 || video.ended) {
    if (++preview.index < preview.ranges.length) { video.currentTime = preview.ranges[preview.index].start; if (video.paused) video.play().catch(error => notify(error.message,true)); }
    else { video.pause(); stopAssembly(); }
  } else if (video.currentTime < part.start) video.currentTime = part.start;
  return true;
}
function assemblyRanges() {
  const ranges = requireProject().edit_ranges || [];
  if (!ranges.length || ranges.length > 32) throw new Error('拼接清单需有 1–32 段。');
  return ranges.map(r => validRange(r.start,r.end));
}
function assemblyChanged() { stopAssembly(); markDirty(); renderAssembly(); }
function addAssembly(ranges) {
  const parts = requireProject().edit_ranges || [];
  const additions = ranges.filter(r => !parts.some(p => p.start === r.start && p.end === r.end));
  if (parts.length + additions.length > 32) throw new Error('每个成片最多保留 32 段，请分成多个作品。');
  state.project.edit_ranges = [...parts,...clone(additions)]; assemblyChanged();
}
function renderAssembly() {
  const parts = requireProject().edit_ranges || [];
  $('assembly-summary').textContent = `${parts.length} 段 · 成片 ${(parts.reduce((s,r) => s+r.end-r.start,0)).toFixed(1)} 秒 · 修改自动保存。可先查看一段、在原片设置插话的入点与出点，再回来「剔除当前选区」。`;
  let offset = 0;
  $('assembly-list').innerHTML = parts.map((part,index) => {
    const outputTime = offset; offset += part.end-part.start;
    return `<div class="assembly-row"><strong>${index+1}<small>成片 ${shortTime(outputTime)}</small></strong><label>原片入点<input data-index="${index}" data-part-field="start" aria-label="第${index+1}段入点" value="${timecode(part.start)}"></label><label>原片出点<input data-index="${index}" data-part-field="end" aria-label="第${index+1}段出点" value="${timecode(part.end)}"></label><div>${[['view','查看 / 翻译'],['up','↑'],['down','↓'],['delete','移除']].map(([action,label]) => `<button class="button quiet tiny" data-index="${index}" data-part-action="${action}" aria-label="第${index+1}段${label}">${label}</button>`).join('')}</div></div>`;
  }).join('') || '<p class="muted">先把候选故事或当前选区加入清单。</p>';
}
function exportRanges() { return $('export-scope').value === 'assembly' ? assemblyRanges() : [validRange()]; }
function renderExportRange() {
  const p = requireProject(), height = Number($('export-height').value), video = ['video','burn'].includes($('export-format').value);
  $('export-height').disabled = !video;
  $('export-quality').textContent = video ? `原片 ${p.width} × ${p.height}；${height && height < p.height ? `输出高度 ${height}，宽度按比例缩放` : '保留原片清晰度，不放大低清素材'}。` : '独立字幕文件不改变视频分辨率。';
  const batch = $('export-scope').value === 'batch';
  $('export-name').disabled = batch;
  if (batch) { $('export-range').textContent = `${savedClips().length} 个切片 · 分别导出，使用各自名称`; return; }
  const parts = exportRanges();
  $('export-range').textContent = `${parts.length} 段 · 共 ${(parts.reduce((s,r) => s+r.end-r.start,0)).toFixed(1)} 秒`;
}
function openExport(scope) { commitClipInputs(); $('export-scope').value = scope; renderExportRange(); showDialog('export-dialog'); }

function readSpeakerSettings() {
  return [...$('speaker-settings').children].map(row => ({id:row.dataset.speaker,name:row.querySelector('[data-speaker-name]').value.trim(),color:row.querySelector('[data-speaker-color]').value}));
}
function renderSpeakerSettings(speakers) {
  $('speaker-settings').innerHTML = speakers.map(person => `<div class="speaker-setting" data-speaker="${escapeHtml(person.id)}"><label>角色名称<input data-speaker-name maxlength="40" required aria-label="角色名称 ${escapeHtml(person.name)}" value="${escapeHtml(person.name)}"></label><label>字体颜色<input type="color" data-speaker-color aria-label="字体颜色 ${escapeHtml(person.name)}" value="${person.color}"></label><span style="color:${person.color}">字幕示例 · 彼方！</span></div>`).join('');
}
function bindEvents() {
  $('clips-button').onclick = handle(openSavedClips);
  $('assembly-save').onclick = handle(() => { closeDialog('assembly-dialog'); openSavedClips(); $('clip-save-source').value = 'assembly'; });
  $('clip-save-target').onchange = () => { const h = savedClips().find(h=>h.id===$('clip-save-target').value); $('clip-title').value = h?.title || ''; };
  $('clip-save-form').onsubmit = handle(async event => {
    event.preventDefault(); commitClipInputs();
    const h = saveNamedClip($('clip-title').value,$('clip-save-source').value === 'assembly' ? assemblyRanges() : [validRange()],$('clip-save-target').value);
    markDirty(); await flushSave(); renderCandidates(); renderSavedClips(); $('clip-save-target').value = h.id; notify('切片已保存；字幕校对内容继续共用。');
  });
  $('saved-clips').onclick = handle(async event => {
    const edit = event.target.closest('[data-edit-clip]'), output = event.target.closest('[data-export-clip]');
    if (!edit && !output) return;
    const h = await loadSavedClip(edit?.dataset.editClip || output.dataset.exportClip);
    closeDialog('clips-dialog');
    if (output) { $('export-name').value = h.title; openExport('assembly'); }
    else notify('已载入切片；修改选段后，到「我的切片」更新保存。');
  });
  $('clips-export').onclick = handle(() => { closeDialog('clips-dialog'); openExport('batch'); });
  $('speakers-button').onclick = handle(() => { requireProject(); renderSpeakerSettings(projectSpeakers()); showDialog('speakers-dialog'); });
  $('speaker-add').onclick = handle(() => { const speakers = readSpeakerSettings(); if (speakers.length >= 24) throw new Error('最多定义 24 位角色。'); renderSpeakerSettings([...speakers,{id:uid(),name:'新角色',color:'#FFFFFF'}]); });
  $('speaker-settings').oninput = event => { if (event.target.matches('[data-speaker-color]')) event.target.closest('.speaker-setting').querySelector('span').style.color = event.target.value; };
  $('speakers-form').onsubmit = handle(async event => { event.preventDefault(); const speakers = readSpeakerSettings(); if (speakers.some(s => !s.name || !/^#[0-9a-f]{6}$/i.test(s.color))) throw new Error('请填写角色名与颜色。'); state.project.speakers = speakers; markDirty(); await flushSave(); closeDialog('speakers-dialog'); renderSubtitles(); renderOverlay(); });
  $('subtitle-list').addEventListener('change',handle(event => { const input = event.target.closest('[data-speaker-id]'); if (!input) return; const cue = state.project.segments.find(s => s.id === input.closest('.subtitle-row').dataset.id); const ids = cue.speaker_ids || []; if (input.checked && ids.length >= 8) { input.checked = false; throw new Error('每句最多选择 8 位说话人。'); } checkpoint(); cue.speaker_ids = input.checked ? [...ids,input.dataset.speakerId] : ids.filter(id => id !== input.dataset.speakerId); cue.reviewed = false; changedSegments(); }));
  $('story-mode').onchange = () => { state.project.settings.story_mode = $('story-mode').value; markDirty(); };
  $('assembly-button').onclick = handle(() => { requireProject(); renderAssembly(); showDialog('assembly-dialog'); });
  $('assembly-add').onclick = handle(() => { commitClipInputs(); addAssembly([validRange()]); });
  $('assembly-cut').onclick = handle(() => { commitClipInputs(); const parts = subtractRange(state.project.edit_ranges || [],validRange()); if (parts.length > 32) throw new Error('剔除后超过 32 段，请先减少片段。'); state.project.edit_ranges = parts; assemblyChanged(); });
  $('assembly-preview').onclick = handle(async () => { const ranges = assemblyRanges(); stopAssembly(); state.preview = {ranges:clone(ranges),index:0}; $('loop-check').checked = false; closeDialog('assembly-dialog'); seek(ranges[0].start); await $('video').play(); notify('正在连播拼接清单；可在「拼接成片」中停止。'); });
  $('assembly-stop').onclick = () => { stopAssembly(); $('video').pause(); };
  $('assembly-export').onclick = handle(() => { assemblyRanges(); closeDialog('assembly-dialog'); openExport('assembly'); });
  $('assembly-list').onchange = handle(event => { const input = event.target.closest('[data-part-field]'); if (!input) return; const index = Number(input.dataset.index); let part; try { part = {...state.project.edit_ranges[index], [input.dataset.partField]:parseTime(input.value)}; validRange(part.start,part.end); } catch (error) { input.value = timecode(state.project.edit_ranges[index][input.dataset.partField]); throw error; } state.project.edit_ranges[index] = part; assemblyChanged(); });
  $('assembly-list').onclick = handle(event => { const button = event.target.closest('[data-part-action]'); if (!button) return; const index = Number(button.dataset.index), parts = state.project.edit_ranges, action = button.dataset.partAction;
    if (action === 'view') { stopAssembly(); const part = parts[index]; setClip(part.start,part.end,true); closeDialog('assembly-dialog'); $('subtitle-filter').value = 'clip'; renderSubtitles(); return; }
    if (action === 'delete') parts.splice(index,1);
    else { const other = index + (action === 'up' ? -1 : 1); if (other >= 0 && other < parts.length) [parts[index],parts[other]] = [parts[other],parts[index]]; }
    assemblyChanged();
  });
  for (const id of ['import-button','empty-import']) $(id).onclick = () => showDialog('import-dialog');
  for (const button of document.querySelectorAll('.close-dialog')) button.onclick = () => button.closest('dialog').close();
  for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click',(event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  $('projects').onclick = handle(async (event) => { const button = event.target.closest('[data-project]'); if (button && button.dataset.project !== state.project?.id) await selectProject(button.dataset.project); });
  $('help-button').onclick = () => showDialog('help-dialog');
  $('focus-button').onclick = () => {
    const enabled = document.body.classList.toggle('focus-mode');
    $('focus-button').textContent = enabled ? '退出专注' : '专注校对';
    $('focus-button').ariaPressed = String(enabled);
    window.scrollTo({top:0,behavior:'auto'});
    requestAnimationFrame(drawTimeline);
  };
  $('system-button').onclick = handle(async () => { showDialog('system-dialog'); await loadSystem(); });
  $('demo-button').onclick = handle(async () => { $('demo-button').disabled = true; try { const p = await api('/api/demo'); await selectProject(p); notify('这是合成演示素材，可自由体验剪辑和打轴。'); } finally { $('demo-button').disabled = false; } });
  $('native-pick-button').onclick = handle(async () => { $('native-pick-button').disabled = true; $('import-status').textContent = '请在系统文件选择器中选择视频…'; try { const result = await api('/api/pick-file',{method:'POST'}); if (result.path) await importPath(result.path,$('name-input').value.trim()); } finally { $('native-pick-button').disabled = false; $('import-status').textContent = ''; } });
  $('import-form').onsubmit = handle(async (event) => { event.preventDefault(); const path = $('path-input').value.trim(); if (!path) throw new Error('请填写视频的绝对路径。'); await importPath(path,$('name-input').value.trim()); });
  $('upload-input').onchange = handle(async (event) => { const file = event.target.files[0]; if (!file) return; $('import-status').textContent = `正在复制 ${file.name}…`; const data = new FormData(); data.append('file',file); try { const p = await api('/api/upload',{method:'POST',body:data}); closeDialog('import-dialog'); await selectProject(p); } finally { $('import-status').textContent = ''; event.target.value = ''; } });
  $('youtube-import-button').onclick = handle(async () => { const url = $('youtube-url').value.trim(); if (!url) throw new Error('请填写 YouTube 视频链接。'); $('youtube-import-button').disabled = true; try { const job = await api('/api/sources/youtube',{method:'POST',body:{url,max_height:Number($('youtube-height').value)}}); state.youtubeJobId = job.id; state.jobs.unshift(job); state.jobStates.set(job.id,job.status); $('youtube-status').textContent = '已加入下载队列，下载完成后会自动创建项目。'; $('youtube-cancel-button').classList.remove('hidden'); renderJobs(); } catch (error) { $('youtube-import-button').disabled = false; throw error; } });
  $('youtube-cancel-button').onclick = handle(async () => { if (state.youtubeJobId) { await api(`/api/jobs/${state.youtubeJobId}/cancel`,{method:'POST'}); await pollJobs(); } });
  $('mode-select').onchange = () => { state.project.settings.mode = $('mode-select').value; markDirty(); };
  for (const id of ['clip-min','clip-max']) {
    $(id).oninput = () => { $(id).dataset.dirty = 'true'; };
    $(id).onchange = handle(commitCandidateSettings);
    $(id).onblur = handle(commitCandidateSettings);
    $(id).onkeydown = handle((event) => { if (event.key === 'Enter') { event.preventDefault(); commitCandidateSettings(); $(id).blur(); } });
  }
  $('scan-button').onclick = handle(async () => { commitCandidateSettings(); await startJob('scan'); });
  $('waveform-button').onclick = handle(async () => { await startJob('waveform'); });
  $('semantic-button').onclick = handle(async () => { if (!requireProject().segments.length) throw new Error('语义选片需要字幕，请先识别日语或导入 SRT。'); commitCandidateSettings(); await startJob('semantic',{story_mode:$('story-mode').value}); });
  $('asr-button').onclick = handle(() => { commitClipInputs(); $('asr-model').value = state.project.settings.asr_model || 'turbo'; showDialog('asr-dialog'); });
  $('asr-form').onsubmit = handle(async (event) => { event.preventDefault(); const options = {model:$('asr-model').value,sentence_grouping:$('asr-sentence-grouping').checked,...($('asr-scope').value === 'clip' ? validRange() : {})}; state.project.settings.asr_model = options.model; markDirty(); await startJob('transcribe',options); closeDialog('asr-dialog'); });
  $('translate-button').onclick = handle(() => { if (!requireProject().segments.length) throw new Error('还没有字幕，请先识别日语或导入 SRT。'); commitClipInputs(); showDialog('translate-dialog'); });
  $('translation-provider').onchange = () => $('api-fields').classList.toggle('hidden',$('translation-provider').value === 'local');
  $('translate-form').onsubmit = handle(async (event) => {
    event.preventDefault(); const provider = $('translation-provider').value;
    const options = {provider,overwrite:$('overwrite-translation').checked,...($('translation-scope').value === 'clip' ? validRange() : {})};
    if (provider === 'openai-compatible') { options.api_base = $('api-base').value.trim(); options.api_model = $('api-model').value.trim(); options.api_key = $('api-key').value; if (!options.api_base || !options.api_model) throw new Error('请填写 API Base URL 和模型名称。'); const url = new URL(options.api_base); if (!['https:','http:'].includes(url.protocol)) throw new Error('API 地址需要使用 https:// 或 http://。'); }
    await startJob('translate',options); closeDialog('translate-dialog');
  });
  $('export-button').onclick = handle(() => openExport('clip'));
  $('export-scope').onchange = handle(renderExportRange);
  $('export-format').onchange = handle(renderExportRange);
  $('export-height').onchange = handle(renderExportRange);
  $('export-form').onsubmit = handle(async (event) => { event.preventDefault(); if ($('export-scope').value === 'batch') { const button = event.target.querySelector('[type=submit]'); button.disabled = true; try { await batchExport(); closeDialog('export-dialog'); } finally { button.disabled = false; } return; } const ranges = exportRanges(); await startJob('export',{ranges,format:$('export-format').value,output_height:Number($('export-height').value),language:$('export-language').value,name:$('export-name').value.trim() || undefined}); closeDialog('export-dialog'); });
  $('source-button').onclick = () => { const p = state.project; $('source-name').value = p.name; $('source-url').value = p.source_url || ''; $('source-title').value = p.source_title || ''; $('glossary').value = p.settings.glossary || ''; showDialog('source-dialog'); };
  $('source-form').onsubmit = handle(async (event) => { event.preventDefault(); const name = $('source-name').value.trim(); if (!name) throw new Error('项目名称不能为空。'); Object.assign(state.project,{name,source_url:$('source-url').value.trim(),source_title:$('source-title').value.trim()}); state.project.settings.glossary = $('glossary').value.trim(); markDirty(); await flushSave(); closeDialog('source-dialog'); updateProjectHeading(); await loadProjects(); });
  $('import-srt-button').onclick = () => showDialog('srt-dialog');
  $('srt-file').onchange = handle(async (event) => { if (event.target.files[0]) $('srt-text').value = await event.target.files[0].text(); });
  $('srt-form').onsubmit = handle(async (event) => { event.preventDefault(); const text = $('srt-text').value.trim(); if (!text) throw new Error('请选择 SRT 文件或粘贴字幕内容。'); await flushSave(); checkpoint(); const p = await api(`/api/projects/${state.project.id}/subtitles`,{method:'POST',body:{text,language:$('srt-language').value,revision:state.project.revision}}); state.project = clone(p); state.base = clone(p); state.subtitlePage = 0; renderProject(); closeDialog('srt-dialog'); notify('字幕已导入。'); });
  $('play-button').onclick = handle(togglePlay); $('back-button').onclick = handle(() => seek($('video').currentTime-2)); $('forward-button').onclick = handle(() => seek($('video').currentTime+2));
  $('video').onclick = handle(togglePlay);
  $('video').addEventListener('timeupdate',() => { if (advanceAssembly()) { renderPlayback(); return; } if (state.project && $('loop-check').checked && !$('video').paused && $('video').currentTime >= state.clip.end) seek(state.clip.start); ensureView($('video').currentTime); renderPlayback(); });
  for (const name of ['play','pause','seeked','loadedmetadata']) $('video').addEventListener(name,renderPlayback);
  $('video').addEventListener('error',() => { if (state.project) $('media-error').classList.remove('hidden'); });
  $('proxy-button').onclick = handle(async () => { await startJob('proxy'); });
  $('rate-select').onchange = () => { $('video').playbackRate = Number($('rate-select').value); };
  $('set-in-button').onclick = handle(() => setClip($('video').currentTime,state.clip.end)); $('set-out-button').onclick = handle(() => setClip(state.clip.start,$('video').currentTime)); $('full-range-button').onclick = handle(() => { state.activeHighlight = null; setClip(0,state.project.duration); renderCandidates(); });
  for (const id of ['clip-start','clip-end']) {
    $(id).oninput = () => { $(id).dataset.dirty = 'true'; };
    $(id).onchange = handle(commitClipInputs);
    $(id).onblur = handle(commitClipInputs);
    $(id).onkeydown = handle((event) => { if (event.key === 'Enter') { event.preventDefault(); commitClipInputs(); $(id).blur(); } });
  }
  $('candidates').onclick = handle((event) => { const assemble = event.target.closest('[data-assemble]'); if (assemble) { const h = state.project.highlights.find(h => h.id === assemble.dataset.assemble); addAssembly(h.ranges || [{start:h.start,end:h.end}]); showDialog('assembly-dialog'); return; } const retain = event.target.closest('[data-retain]'); if (retain) { const h = state.project.highlights.find((h) => h.id === retain.dataset.retain); h.selected = !h.selected; markDirty(); renderCandidates(); return; } const card = event.target.closest('[data-highlight]'); if (card) chooseHighlight(card.dataset.highlight); });
  $('candidates').onkeydown = handle((event) => { if (event.key === 'Enter' && event.target.dataset.highlight) chooseHighlight(event.target.dataset.highlight); });
  $('subtitle-filter').onchange = () => { state.subtitlePage = 0; renderSubtitles(); };
  $('subtitle-list').addEventListener('focusin',(event) => { const row = event.target.closest('.subtitle-row'); if (row) { selectSegment(row.dataset.id); if (event.target.matches('textarea,input')) checkpoint(); } });
  $('subtitle-list').addEventListener('input',(event) => { if (event.target.matches('input[data-field]')) { event.target.dataset.dirty = 'true'; return; } if (!event.target.matches('textarea')) return; const row = event.target.closest('.subtitle-row'), segment = state.project.segments.find((s) => s.id === row.dataset.id); segment[event.target.dataset.field] = event.target.value; segment.reviewed = false; row.querySelector('.review-toggle').classList.remove('reviewed'); markDirty(); renderOverlay(); });
  $('subtitle-list').addEventListener('change',handle((event) => { if (event.target.matches('input[data-field]')) commitSubtitleTime(event.target); }));
  $('subtitle-list').addEventListener('focusout',handle((event) => { if (event.target.matches('input[data-field]')) commitSubtitleTime(event.target); }));
  $('subtitle-list').addEventListener('keydown',handle((event) => { if (event.key === 'Enter' && event.target.matches('input[data-field]')) { event.preventDefault(); commitSubtitleTime(event.target); event.target.blur(); } }));
  $('subtitle-list').addEventListener('click',handle((event) => { const row = event.target.closest('.subtitle-row'); if (!row) return; selectSegment(row.dataset.id); const action = event.target.closest('[data-row-action]')?.dataset.rowAction; if (action === 'seek') seek(selectedSegment().start); if (action === 'pin-start' || action === 'pin-end') { event.preventDefault(); pinSubtitleTime(action === 'pin-start' ? 'start' : 'end'); } if (action === 'review') { checkpoint(); const s = selectedSegment(); s.reviewed = !s.reviewed; markDirty(); renderSubtitles(); } }));
  $('add-subtitle').onclick = handle(addSubtitle); $('split-subtitle').onclick = handle(splitSubtitle); $('merge-subtitle').onclick = handle(mergeSubtitle); $('delete-subtitle').onclick = handle(deleteSubtitle); $('undo-subtitle').onclick = handle(undoSubtitle);
  $('shift-subtitle').onclick = handle(() => { commitClipInputs(); $('shift-offset').value = '0.000'; renderShiftPreview(); showDialog('shift-dialog'); });
  $('sentence-subtitle').onclick = handle(async () => { showDialog('sentence-dialog'); await previewSentences(); });
  $('sentence-refresh').onclick = handle(previewSentences);
  for (const id of ['sentence-scope','sentence-duration','sentence-chars','sentence-gap']) $(id).onchange = () => { sentencePreview = null; $('sentence-apply').disabled = true; $('sentence-summary').textContent = '参数已变更，请重新预览。'; };
  $('sentence-form').onsubmit = handle(async event => { event.preventDefault(); await applySentences(); });
  $('shift-offset').oninput = renderShiftPreview; $('shift-scope').onchange = renderShiftPreview;
  $('shift-form').onsubmit = handle(async (event) => {
    event.preventDefault(); const {offset,timings} = shiftPlan(), byId = new Map(timings.map((s) => [s.id,s]));
    checkpoint(); for (const s of state.project.segments) { const shifted = byId.get(s.id); if (shifted) { s.start = shifted.start; s.end = shifted.end; } }
    changedSegments(); closeDialog('shift-dialog'); await flushSave(); notify(`已将 ${timings.length} 条字幕${offset > 0 ? '延后' : '提前'} ${Math.abs(offset).toFixed(3)} 秒。`);
  });
  document.querySelector('.subtitles-panel').addEventListener('click',handle((event) => {
    const button = event.target.closest('[data-recovery]'); if (!button) return;
    const entry = state.recovery.find((r) => r.id === button.dataset.recovery); if (!entry) return;
    if (button.dataset.recoveryAction === 'download') {
      const text = `${timecode(entry.segment.start)} --> ${timecode(entry.segment.end)}\n日文：${entry.segment.ja || ''}\n中文：${entry.segment.zh || ''}\n`;
      const url = URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'})), link = document.createElement('a'); link.href = url; link.download = `字幕编辑备份-${entry.id}.txt`; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000); return;
    }
    if (button.dataset.recoveryAction === 'restore') {
      checkpoint(); const s = {...clone(entry.segment),id:uid(),reviewed:false,flags:[...(entry.segment.flags || []),'恢复编辑，请检查重叠']}; validRange(s.start,s.end); state.project.segments.push(s); state.selectedId = s.id; changedSegments();
    }
    state.recovery = state.recovery.filter((r) => r.id !== entry.id); persistRecovery(); renderRecovery();
  }));
  $('zoom-select').onchange = () => { state.zoom = Number($('zoom-select').value); state.viewStart = Math.max(0,$('video').currentTime-state.zoom/2); drawTimeline(); };
  $('timeline').addEventListener('pointerdown',handle((event) => {
    if (!state.project) return; const canvas = $('timeline'), rect = canvas.getBoundingClientRect(), {start,span} = timelineBounds(), s = state.project.segments.find((s) => s.id === state.selectedId);
    const localY = (event.clientY-rect.top)/rect.height*112;
    if (s && localY > 77) { const localX = event.clientX-rect.left; const startX = (s.start-start)/span*rect.width, endX = (s.end-start)/span*rect.width;
      const edge = Math.abs(localX-startX) < 9 ? 'start' : Math.abs(localX-endX) < 9 ? 'end' : null;
      if (edge) { checkpoint(); state.drag = {id:s.id,edge}; canvas.setPointerCapture(event.pointerId); return; }
    }
    const t = timelineTime(event); if (localY > 80) { const under = state.project.segments.find((s) => s.start <= t && s.end >= t); if (under) selectSegment(under.id,false,true); } seek(t); state.drag = {seek:true}; canvas.setPointerCapture(event.pointerId);
  }));
  $('timeline').addEventListener('pointermove',(event) => { if (!state.drag) return; const t = timelineTime(event); if (state.drag.seek) { seek(t); return; } const s = state.project.segments.find((s) => s.id === state.drag.id); if (!s) return; s[state.drag.edge] = Math.round((state.drag.edge === 'start' ? Math.min(t,s.end-0.05) : Math.max(t,s.start+0.05))*1000)/1000; s.reviewed = false; renderSubtitles(); drawTimeline(); renderOverlay(); });
  for (const name of ['pointerup','pointercancel']) $('timeline').addEventListener(name,() => { if (state.drag && !state.drag.seek) changedSegments(); state.drag = null; });
  $('timeline').addEventListener('wheel',(event) => { if (!state.zoom) return; event.preventDefault(); state.viewStart += (event.deltaX || event.deltaY)/300*state.zoom*0.2; drawTimeline(); },{passive:false});
  $('job-panel').onclick = handle(async (event) => { const button = event.target.closest('[data-job]'); if (!button) return; if (button.dataset.jobAction === 'dismiss') { state.dismissedJobs.add(button.dataset.job); renderJobs(); } else { await api(`/api/jobs/${button.dataset.job}/cancel`,{method:'POST'}); await pollJobs(); notify('已请求取消任务。'); } });
  $('model-list').onclick = handle(async (event) => { const button = event.target.closest('[data-model]'); if (!button || button.disabled) return; button.disabled = true; try { const job = await api(`/api/models/${button.dataset.model}/download`,{method:'POST',body:{endpoint:$('model-endpoint').value}}); job.model_id ||= button.dataset.model; state.jobs.unshift(job); state.jobStates.set(job.id,job.status); renderJobs(); renderSystem(); notify('模型下载已加入任务队列。'); } catch (error) { button.disabled = false; throw error; } });
  $('system-dialog').addEventListener('click',handle(async (event) => { const button = event.target.closest('[data-download-job]'); if (!button) return; if (button.dataset.status === 'failed') state.dismissedJobs.add(button.dataset.downloadJob); else await api(`/api/jobs/${button.dataset.downloadJob}/cancel`,{method:'POST'}); await pollJobs(); renderSystem(); }));
  $('cleanup-button').onclick = handle(async () => { requireProject(); const active = state.jobs.some((j) => j.project_id === state.project.id && ['queued','running'].includes(j.status)); if (active) throw new Error('请等待当前项目的处理任务完成后再清理缓存。'); await api(`/api/projects/${state.project.id}/cleanup`,{method:'POST'}); await refreshCurrentProject(); await loadSystem(); notify('可重建缓存已清理。'); });
  $('save-status').onclick = handle(flushSave);
  window.addEventListener('resize',drawTimeline);
  window.addEventListener('beforeunload',(event) => { if (state.project && Object.keys(patchFor(state.project,state.base)).length > 1) { event.preventDefault(); event.returnValue = ''; } });
  document.addEventListener('keydown',handle(async (event) => {
    const editing = event.target.matches('input,textarea,select,[contenteditable=true]');
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); await flushSave(); return; }
    if (editing || document.querySelector('dialog[open]')) return;
    if (event.key === '?') { showDialog('help-dialog'); return; } if (!state.project) return;
    if (event.altKey && !event.metaKey && !event.ctrlKey && ['KeyI','KeyO'].includes(event.code)) { event.preventDefault(); pinSubtitleTime(event.code === 'KeyI' ? 'start' : 'end'); return; }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); undoSubtitle(); return; }
    if (event.key === ' ') { event.preventDefault(); await togglePlay(); }
    if (['ArrowLeft','ArrowRight'].includes(event.key)) { event.preventDefault(); seek($('video').currentTime+(event.key === 'ArrowLeft' ? -1 : 1)*(event.shiftKey ? 0.1 : 2)); }
    if (event.key.toLowerCase() === 'i') setClip($('video').currentTime,state.clip.end);
    if (event.key.toLowerCase() === 'o') setClip(state.clip.start,$('video').currentTime);
    if (['[',']'].includes(event.key)) { const segments = filteredSegments(); let i = segments.findIndex((s) => s.id === state.selectedId); if (i < 0) i = event.key === ']' ? -1 : segments.length; const s = segments[Math.max(0,Math.min(segments.length-1,i+(event.key === '[' ? -1 : 1)))]; if (s) selectSegment(s.id,true,true); }
  }));
}

async function init() {
  try { const saved = JSON.parse(localStorage.getItem('kotori.editor-recovery.v1') || '[]'); if (Array.isArray(saved)) state.recovery = saved.filter((r) => r?.id && r?.projectId && r?.segment?.id); } catch (_) { /* Start normally if a browser disables local storage. */ }
  bindEvents();
  const results = await Promise.allSettled([loadProjects(),loadSystem(),pollJobs()]);
  if (results[0].status === 'rejected') { notify('无法连接本地服务，请通过「启动烤肉工房.command」启动应用。',true); return; }
  const id = location.hash.slice(1);
  if (id && state.projects.some((p) => p.id === id)) { try { await selectProject(id); } catch (error) { notify(error.message,true); } }
  setInterval(pollJobs,2500);
  setInterval(() => { if (!document.hidden) loadSystem().catch(() => {}); },45000);
}
init();
