/* Run with: node --test tests/frontend.test.cjs
 * Pure editor and save logic use Node's VM; no browser or third-party packages.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8')
  .replace(/\ninit\(\);\s*$/, '');

function harness() {
  const nodes = {
    video: { currentTime: 1.25 },
    'shift-scope': { value: 'all' },
    'shift-offset': { value: '0.250' },
    'clip-min': { value: '30', dataset: {} },
    'clip-max': { value: '120', dataset: {} },
  };
  const storage = new Map();
  const notifications = [];
  const statuses = [];
  const context = vm.createContext({
    structuredClone,
    crypto: webcrypto,
    console,
    setTimeout,
    clearTimeout,
    document: { getElementById: (id) => nodes[id] || null },
    localStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
    },
    notifySink: (message, error) => notifications.push({ message, error }),
    saveStatusSink: (message, pending) => statuses.push({ message, pending }),
  });
  const run = (code) => vm.runInContext(code, context);
  run(source);
  // DOM rendering is outside these tests; actual save/merge/timing code remains intact.
  run(`
    notify = notifySink;
    setSaveStatus = saveStatusSink;
    renderSubtitles = () => {};
    updateProjectHeading = () => {};
  `);
  return { run, nodes, storage, notifications, statuses };
}

function seedProject(run) {
  run(`
    state.base = {
      id: 'project-a', revision: 0, duration: 10,
      settings: { mode: 'mixed', glossary: '' },
      segments: [
        { id: 'a', start: 1, end: 2, ja: '日文', zh: '中文', reviewed: true },
        { id: 'b', start: 4, end: 6, ja: 'second', zh: '', reviewed: false }
      ]
    };
    state.project = clone(state.base);
    state.selectedId = 'a';
    state.clip = { start: 3, end: 7 };
  `);
}

test('timecode preserves milliseconds and carries rounding across minute/hour boundaries', () => {
  const { run } = harness();
  assert.equal(run('timecode(3661.123)'), '01:01:01.123');
  assert.equal(run('timecode(59.9996)'), '00:01:00.000');
  assert.equal(run('timecode(3599.9996)'), '01:00:00.000');
  assert.equal(run('timecode(3661.123, false)'), '01:01:01');
});

test('time input accepts supported formats and rejects malformed or overflowing fields', () => {
  const { run } = harness();
  for (const value of ['01:01:01,123', '01:01:01.123', '3661.123']) {
    assert.equal(run(`parseTime(${JSON.stringify(value)})`), 3661.123);
  }
  assert.equal(run("parseTime('01:23.450')"), 83.45);
  for (const value of ['', '-1', '00:60:01', '01:23:60', '1.1234', 'NaN', '1:2:3:4']) {
    assert.throws(() => run(`parseTime(${JSON.stringify(value)})`));
  }
});

test('clip boundaries reject negative, reversed, empty and out-of-video ranges', () => {
  const { run } = harness();
  seedProject(run);
  assert.equal(run('JSON.stringify(validRange(0, 10))'), '{"start":0,"end":10}');
  for (const code of ['validRange(-1, 4)', 'validRange(4, 2)', 'validRange(2, 2)', 'validRange(0, 11)', 'validRange(NaN, 4)']) {
    assert.throws(() => run(code));
  }
});

test('splitting Japanese text does not lose characters or create empty halves unnecessarily', () => {
  const { run } = harness();
  assert.equal(run("splitText('テスト字幕', 0.5).join('')"), 'テスト字幕');
  assert.equal(run("splitText('テスト字幕', 0.5).filter(Boolean).length"), 2);
  assert.equal(run("JSON.stringify(splitText('', 0.5))"), '["",""]');
});

test('field-level merge retains manual Japanese edits and remote Chinese translation', () => {
  const { run } = harness();
  const merged = run(`JSON.stringify(mergeRecords(
    [{id:'a', start:0, ja:'old', zh:'remote translation'}],
    [{id:'a', start:0, ja:'old', zh:''}],
    [{id:'a', start:0, ja:'typed Japanese', zh:''}]
  ))`);
  assert.deepEqual(JSON.parse(merged), [{ id: 'a', start: 0, ja: 'typed Japanese', zh: 'remote translation' }]);
});

test('merge preserves explicit local deletion and unrelated remote additions', () => {
  const { run } = harness();
  assert.equal(run(`JSON.stringify(mergeRecords(
    [{id:'a', start:0, ja:'remote edit'}, {id:'b', start:1, ja:'new remote cue'}],
    [{id:'a', start:0, ja:'old'}],
    []
  ))`), '[{"id":"b","start":1,"ja":"new remote cue"}]');
});

test('merge combines independently added cues and does not resurrect remotely deleted cues', () => {
  const { run } = harness();
  assert.equal(run(`JSON.stringify(mergeRecords(
    [{id:'b', start:1}],
    [{id:'a', start:0}],
    [{id:'a', start:0}, {id:'c', start:2}]
  ).map(s => s.id))`), '["b","c"]');
});

test('deleted cue with unsaved typed text is preserved as a recoverable local draft, not restored into timeline', () => {
  const { run, storage, notifications } = harness();
  const merged = run(`JSON.stringify(mergeProject(
    {id:'p', revision:2, segments:[]},
    {id:'p', revision:1, segments:[{id:'a', start:0, end:1, ja:'old', zh:''}]},
    {id:'p', revision:1, segments:[{id:'a', start:0, end:1, ja:'typed', zh:'手动翻译'}]}
  ))`);
  assert.deepEqual(JSON.parse(merged).segments, []);
  assert.equal(run('state.recovery.length'), 1);
  assert.equal(run('state.recovery[0].segment.ja'), 'typed');
  assert.equal(run('state.recovery[0].segment.zh'), '手动翻译');
  assert.equal(JSON.parse(storage.get('kotori.editor-recovery.v1'))[0].projectId, 'p');
  assert.equal(notifications.length, 1);
  run("recoverDeletedEdit('p', {id:'a', start:0, end:1, ja:'typed', zh:'手动翻译'})");
  assert.equal(run('state.recovery.length'), 1, 'repeated polling must not duplicate recovery drafts');
});

test('project settings merge independently at field level and keep the remote revision', () => {
  const { run } = harness();
  const merged = JSON.parse(run(`JSON.stringify(mergeProject(
    {revision:2, settings:{mode:'chat', glossary:'new glossary'}},
    {revision:1, settings:{mode:'mixed', glossary:''}},
    {revision:1, settings:{mode:'gaming', glossary:''}}
  ))`));
  assert.deepEqual(merged, { revision: 2, settings: { mode: 'gaming', glossary: 'new glossary' } });
});

test('autosave sends a second revision when a keystroke arrives while the first save is pending', async () => {
  const { run } = harness();
  await run(`(async () => {
    state.base = {id:'p', revision:0, segments:[{id:'a', start:0, end:1, ja:'old', zh:''}]};
    state.project = clone(state.base);
    state.project.segments[0].ja = 'first edit';
    globalThis.saveCalls = [];
    api = async (url, options) => {
      const body = clone(options.body);
      saveCalls.push(body);
      if (saveCalls.length === 1) state.project.segments[0].ja = 'typed during save';
      return {id:'p', revision:body.revision+1, segments:body.segments};
    };
    await flushSave();
  })()`);
  assert.equal(run('saveCalls.length'), 2);
  assert.equal(run('saveCalls[0].segments[0].ja'), 'first edit');
  assert.equal(run('saveCalls[1].segments[0].ja'), 'typed during save');
  assert.equal(run('state.project.segments[0].ja'), 'typed during save');
  assert.equal(run('state.base.revision'), 2);
  assert.equal(run('state.saving'), null);
});

test('409 retry preserves local edits/deletion and newly completed remote translations/additions', async () => {
  const { run } = harness();
  await run(`(async () => {
    state.base = {id:'p', revision:0, segments:[
      {id:'a', start:0, end:1, ja:'old', zh:''},
      {id:'b', start:2, end:3, ja:'delete me', zh:''}
    ]};
    state.project = clone(state.base);
    state.project.segments[0].ja = 'local';
    state.project.segments.pop();
    let patches = 0;
    api = async (url, options) => {
      if (!options) return {id:'p', revision:1, segments:[
        {id:'a', start:0, end:1, ja:'old', zh:'remote'},
        {id:'b', start:2, end:3, ja:'delete me', zh:''},
        {id:'c', start:4, end:5, ja:'new', zh:''}
      ]};
      if (++patches === 1) { const error = new Error('conflict'); error.status = 409; throw error; }
      return {id:'p', revision:2, segments:clone(options.body.segments)};
    };
    await flushSave();
  })()`);
  assert.equal(run('state.project.segments.length'), 2);
  assert.equal(run('state.project.segments[0].ja'), 'local');
  assert.equal(run('state.project.segments[0].zh'), 'remote');
  assert.equal(run('state.project.segments[1].id'), 'c');
  assert.equal(run('state.base.revision'), 2);
});

test('failed save keeps edits and original baseline for retry instead of reporting success', async () => {
  const { run, statuses } = harness();
  seedProject(run);
  run("state.project.segments[0].zh = 'unsaved edit'; api = async () => { throw new Error('offline'); };");
  await assert.rejects(run('flushSave()'), /offline/);
  assert.equal(run('state.project.segments[0].zh'), 'unsaved edit');
  assert.equal(run('state.base.segments[0].zh'), '中文');
  assert.equal(run('state.base.revision'), 0);
  assert.equal(run('state.saving'), null);
  assert.equal(statuses.at(-1).pending, true);
  assert.match(statuses.at(-1).message, /未保存/);
});

test('signed millisecond offsets preserve length and never mutate original subtitles', () => {
  const { run } = harness();
  seedProject(run);
  const before = run('JSON.stringify(state.project.segments)');
  assert.equal(run('JSON.stringify(shiftedTimings(state.project.segments, 0.125, 10))'),
    '[{"id":"a","start":1.125,"end":2.125},{"id":"b","start":4.125,"end":6.125}]');
  assert.equal(run('JSON.stringify(shiftedTimings(state.project.segments, -1, 10))'),
    '[{"id":"a","start":0,"end":1},{"id":"b","start":3,"end":5}]');
  assert.equal(run('JSON.stringify(state.project.segments)'), before);
});

test('offset validation rejects the entire plan for any out-of-range cue, with no partial mutation', () => {
  const { run } = harness();
  seedProject(run);
  const before = run('JSON.stringify(state.project.segments)');
  for (const offset of [-1.001, 4.001, 0, 0.0004, Infinity, NaN]) {
    assert.throws(() => run(`shiftedTimings(state.project.segments, ${String(offset)}, 10)`));
    assert.equal(run('JSON.stringify(state.project.segments)'), before);
  }
  assert.throws(() => run('shiftedTimings([], 0.25, 10)'));
});

test('offset scopes target all cues, cues overlapping the clip, or only the selected cue', () => {
  const { run, nodes } = harness();
  seedProject(run);
  assert.equal(run('shiftPlan().timings.length'), 2);
  nodes['shift-scope'].value = 'clip';
  assert.equal(run('JSON.stringify(shiftPlan().timings.map(s => s.id))'), '["b"]');
  nodes['shift-scope'].value = 'selected';
  assert.equal(run('JSON.stringify(shiftPlan().timings.map(s => s.id))'), '["a"]');
  run('state.selectedId = null');
  assert.throws(() => run('shiftPlan()'), /选择一条字幕/);
});

test('pinning a cue to the playhead preserves text and review state and rejects reversed timing', () => {
  const { run, nodes } = harness();
  seedProject(run);
  run('checkpoint=()=>{}; changedSegments=()=>{}; pinSubtitleTime("start");');
  assert.equal(run('state.project.segments[0].start'), 1.25);
  assert.equal(run('state.project.segments[0].reviewed'), true);
  assert.equal(run('state.project.segments[0].ja'), '日文');
  assert.equal(run('state.project.segments[0].zh'), '中文');
  nodes.video.currentTime = 0.9;
  assert.throws(() => run('pinSubtitleTime("end")'));
  assert.equal(run('state.project.segments[0].end'), 2);
  nodes.video.currentTime = 2.125;
  run('pinSubtitleTime("end")');
  assert.equal(run('state.project.segments[0].end'), 2.125);
  assert.equal(run('state.project.segments[0].reviewed'), true);
});

test('visible candidate durations commit together and reject an invalid pair without changing saved settings', () => {
  const { run, nodes } = harness();
  seedProject(run);
  run('globalThis.dirtyCount=0; markDirty=()=>{dirtyCount++;}; state.project.settings.clip_min=30; state.project.settings.clip_max=120;');
  nodes['clip-min'].value = '5'; nodes['clip-min'].dataset.dirty = 'true';
  nodes['clip-max'].value = '25'; nodes['clip-max'].dataset.dirty = 'true';
  run('commitCandidateSettings()');
  assert.equal(run('state.project.settings.clip_min'), 5);
  assert.equal(run('state.project.settings.clip_max'), 25);
  assert.equal(run('dirtyCount'), 1);
  assert.equal(nodes['clip-min'].dataset.dirty, undefined);
  assert.equal(nodes['clip-max'].dataset.dirty, undefined);
  run('commitCandidateSettings()');
  assert.equal(run('dirtyCount'), 1, 'blur after change must not queue a redundant save');
  nodes['clip-min'].value = '40';
  assert.throws(() => run('commitCandidateSettings()'), /最长时长不能小于最短时长/);
  assert.equal(run('state.project.settings.clip_min'), 5);
  assert.equal(run('state.project.settings.clip_max'), 25);
});

test('project refresh preserves focused or dirty candidate duration fields while explicit project switch replaces them', () => {
  const { run, nodes } = harness();
  seedProject(run);
  run('state.project.settings.clip_min=30; state.project.settings.clip_max=120;');
  nodes['clip-min'].value = '5'; nodes['clip-min'].dataset.dirty = 'true';
  nodes['clip-max'].value = '25';
  run("document.activeElement=document.getElementById('clip-max'); renderCandidateSettings();");
  assert.equal(nodes['clip-min'].value, '5');
  assert.equal(nodes['clip-max'].value, '25');
  run('renderCandidateSettings(true)');
  assert.equal(Number(nodes['clip-min'].value), 30);
  assert.equal(Number(nodes['clip-max'].value), 120);
  assert.equal(nodes['clip-min'].dataset.dirty, undefined);
});

test('removing an interruption splits retained ranges and preserves playback order', () => {
  const {run} = harness();
  assert.equal(run(`JSON.stringify(subtractRange([{start:10,end:30},{start:100,end:120}],{start:15,end:20}))`), '[{"start":10,"end":15},{"start":20,"end":30},{"start":100,"end":120}]');
  assert.equal(run(`JSON.stringify(subtractRange([{start:10,end:30}],{start:0,end:40}))`), '[]');
});

test('speaker preview supports one colour and per-glyph chorus without HTML injection', () => {
  const {run} = harness();
  assert.match(run(`coloredText('你好',{speaker_ids:['kanata']})`), /#4C88FF/);
  const chorus = run(`coloredText('<彼方>',{speaker_ids:['lamy','korone','azki']})`);
  assert.equal((chorus.match(/class="chorus-letter"/g)||[]).length,4);
  assert.match(chorus,/#94DEFF/); assert.match(chorus,/#C9994A/); assert.match(chorus,/#E65D76/);
  assert.ok(!chorus.includes('<彼方>'));
});

test('merging different speakers refuses before changing either cue', () => {
  const {run} = harness(); seedProject(run);
  run(`state.selectedId = 'a'; state.project.segments[0].speaker_ids = ['kanata']; state.project.segments[1].speaker_ids = ['lamy'];`);
  assert.throws(() => run('mergeSubtitle()'), /说话人不同/);
  assert.equal(run('state.project.segments.length'),2);
});

test('single retained interaction reopens as its clip without mutating saved ranges', () => {
  const {run} = harness();
  assert.equal(run(`JSON.stringify(initialClip({duration:4310,edit_ranges:[{start:2663.1,end:2865.38}]}))`),'[{"start":2663.1,"end":2865.38}]'.slice(1,-1));
  assert.equal(run(`JSON.stringify(initialClip({duration:10,edit_ranges:[]}))`),'{"start":0,"end":10}');
  assert.equal(run(`JSON.stringify(initialClip({duration:10,edit_ranges:[{start:1,end:2},{start:3,end:4}]}))`),'{"start":0,"end":10}');
});

test('multiple named cuts keep independent ordered ranges and shared corrected subtitles', () => {
  const {run,nodes} = harness(); seedProject(run);
  run(`state.project.highlights=[]; const ranges=[{start:6,end:7},{start:1,end:2}]; const first=saveNamedClip('完整互动',ranges); ranges[0].start=0; const second=saveNamedClip('另一件事',[{start:4,end:6}]); saveNamedClip('短版',[{start:6,end:7}],second.id);`);
  assert.equal(run('savedClips().length'),2);
  assert.equal(run('JSON.stringify(savedClips()[0].ranges)'), '[{"start":6,"end":7},{"start":1,"end":2}]');
  assert.equal(run('state.project.segments[0].zh'),'中文');
  assert.throws(()=>run(`saveNamedClip('错误',[{start:0,end:11}])`),/有效/);
  assert.equal(run('savedClips().length'),2);
  nodes['subtitle-filter']={value:'assembly'};
  run('state.project.edit_ranges=[{start:1,end:2},{start:7,end:8}]');
  assert.equal(run('filteredSegments().map(s=>s.id).join()'),'a');
});

test('batch export submits one job per named cut with its own ordered ranges and requested format', async () => {
  const {run,nodes} = harness(); seedProject(run);
  nodes['export-height']={value:'1080'}; nodes['export-format']={value:'ass'}; nodes['export-language']={value:'zh'};
  run(`state.project.highlights=[]; saveNamedClip('A',[{start:1,end:2},{start:6,end:7}]); saveNamedClip('B',[{start:4,end:6}]); const calls=[]; flushSave=async()=>{}; pollJobs=async()=>{}; api=async(path,opts)=>{calls.push(opts.body); return {id:String(calls.length),status:'queued'};};`);
  await run('batchExport()');
  assert.equal(run('calls.length'),2);
  assert.equal(run('calls[0].options.name'),'A');
  assert.equal(run('calls[1].options.name'),'B');
  assert.equal(run('calls[0].options.ranges.length'),2);
  assert.equal(run('calls[1].options.format'),'ass');
  assert.equal(run('calls[1].options.language'),'zh');
});
