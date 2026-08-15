/* データ取り込み画面。プレビュー 列の設定 取り込み先 実行 / 定期登録。 */

let plan = [];          // 列の設定
let previewInfo = null;
// いま選ばれている取り込み元。サーバのファイル（path）か、アップロード（upload）のどちらか。
let source = null;      // {kind:'server'|'upload', path?, upload?, name}

function readOptions() {
    return {
        path: source?.kind === 'server' ? source.path : '',
        upload: source?.kind === 'upload' ? source.upload : null,
        sheet: $('#sheetWrap').classList.contains('hidden') ? null : $('#sheet').value,
        header_row: Math.max(0, parseInt($('#headerRow').value || '1', 10) - 1),
        delimiter: $('#delimiter').value,
    };
}

/* --- 取り込み元フォルダの管理 --------------------------------------------------- */

async function loadDirs() {
    const r = await api('/api/import/dirs', undefined, 'GET');
    const box = $('#dirList');
    box.replaceChildren(...r.dirs.map(d => el('div', {
        class: 'row', style: 'align-items:center;gap:8px;padding:5px 0;'
            + 'border-bottom:1px solid var(--border)',
    },
        el('span', { class: d.ok ? 'badge badge--ok': 'badge badge--err' }, d['状態']),
        el('code', { class: 'grow', title: d['実際のパス'] }, d['設定値']),
        el('span', { class: 'badge' }, d.source === 'env'? '.env': '画面から追加'),
        (r.editable && d.removable) ? el('button', {
            class: 'btn btn--sm btn--danger',
            onclick: async () => {
                if (!confirm(`${d['設定値']} を取り込み元から外しますか？\n（フォルダ自体は削除されません）`)) return;
                await api('/api/import/dirs', { action: 'remove', path: d['設定値'] });
                toast('取り込み元から外しました。');
                loadDirs();
            },
        }, '外す') : null)));
    if (!r.dirs.length) box.append(el('div', { class: 'small muted' }, '登録がありません。'));
}

function wireDirs() {
    const add = $('#addDir');
    if (!add) return;
    const submit = async () => {
        const v = $('#newDir').value.trim();
        if (!v) return;
        add.disabled = true;
        try {
            await api('/api/import/dirs', { action: 'add', path: v });
            $('#newDir').value = '';
            toast('取り込み元フォルダを追加しました。');
            loadDirs();
        } catch (e) { toast(e.message, 'err', 8000); }
        add.disabled = false;
    };
    add.addEventListener('click', submit);
    $('#newDir').addEventListener('keydown', ev => { if (ev.key === 'Enter') submit(); });
}

/* --- サーバのフォルダを辿るダイアログ -------------------------------------------- */

async function openBrowser(path) {
    $('#browser').classList.remove('hidden');
    const list = $('#browserList');
    list.replaceChildren(el('div', { class: 'fsrow' }, el('span', { class: 'spinner' }), '読み込み中...'));
    let r;
    try {
        r = await api('/api/import/browse', { path: path || null });
    } catch (e) {
        list.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
        return;
    }

    $('#crumbs').replaceChildren(...r.crumbs.flatMap((c, i) => [
        i ? el('span', { class: 'muted' }, '/') : null,
        el('button', { onclick: () => openBrowser(c.path) }, c.name),
    ]).filter(Boolean));
    if (!r.crumbs.length) $('#crumbs').replaceChildren(el('span', { class: 'muted' }, '取り込み元フォルダ'));

    const rows = [];
    if (r.parent) {
        rows.push(el('div', { class: 'fsrow', onclick: () => openBrowser(r.parent) },
            icon('back', 'icon--sm'), el('span', { class: 'name' }, '上のフォルダへ')));
    }
    r.dirs.forEach(d => rows.push(el('div', { class: 'fsrow', onclick: () => openBrowser(d.path) },
        icon('folder', 'icon--sm'), el('span', { class: 'name' }, d.name))));
    r.files.forEach(f => rows.push(el('div', {
        class: 'fsrow',
        onclick: () => { chooseServerFile(f.path, f.name); closeBrowser(); },
    },
        icon('file', 'icon--sm'), el('span', { class: 'name' }, f.name),
        el('span', { class: 'meta' }, `${(f.size / 1024).toFixed(0)} KB ・ ${f.mtime}`))));
    if (!rows.length) rows.push(el('div', { class: 'small muted', style: 'padding:12px' },
        'このフォルダには取り込めるファイルがありません。'));
    list.replaceChildren(...rows);
}

function closeBrowser() { $('#browser').classList.add('hidden'); }

/* --- 選択の確定 ----------------------------------------------------------------- */

function showChosen(icon, label, note) {
    $('#chosen').replaceChildren(el('div', { class: 'chosenfile' },
        el('span', {}, icon),
        el('div', { class: 'grow' },
            el('div', { style: 'font-weight:700' }, label),
            note ? el('div', { class: 'small muted' }, note) : null)));
    $('#readOpts').classList.remove('hidden');
}

function chooseServerFile(path, name) {
    source = { kind: 'server', path, name };
    showChosen('', name, path);
    loadPreview();
}

async function chooseLocalFile(file) {
    const fd = new FormData();
    fd.append('file', file);
    showChosen('', file.name, 'アップロード中...');
    try {
        const res = await fetch('/api/import/upload', { method: 'POST', body: fd });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'アップロードに失敗しました');
        source = { kind: 'upload', upload: data.upload, name: data.name };
        showChosen('', data.name,
            `自分のPCから（${(data.size / 1024).toFixed(0)} KB）・定期取り込みには登録できません`);
        loadPreview();
    } catch (e) {
        toast(e.message, 'err', 8000);
        $('#chosen').replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
    }
}

/* --- プレビュー --------------------------------------------------------------- */

async function loadPreview() {
    if (!source) return;
    const area = $('#previewArea');
    area.replaceChildren(el('div', { class: 'card' },
        el('span', { class: 'spinner' }), '読み込み中...'));
    try {
        const r = await api('/api/import/preview', readOptions());
        previewInfo = r;
        plan = r.plan.map(p => ({
            source: p['元の列名'], name: p['列名'], type: p['型'], include: true,
        }));
        // Excel ならシート欄を出す
        const has = (r.sheets || []).length > 0;
        $('#sheetWrap').classList.toggle('hidden', !has);
        $('#sepWrap').classList.toggle('hidden', has);
        if (has && $('#sheet').options.length !== r.sheets.length) {
            $('#sheet').replaceChildren(...r.sheets.map(s => el('option', {}, s)));
        }
        renderPreview(r);
    } catch (e) {
        area.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
    }
}

function renderPreview(r) {
    const dbSelect = el('select', { id: 'dbTarget' },
        el('option', { value: '' }, '＋ 新しいDBを作る'),
        IMP.dbFiles.map(f => el('option', { value: f }, f)));

    const area = $('#previewArea');
    area.replaceChildren(
        el('div', { class: 'card' },
            el('div', { class: 'card__title' }, 'プレビュー'),
            el('div', { class: 'card__desc' },
                `先頭 ${Math.min(30, r.rows.length)} 行 / 読み込んだ ${r.scanned.toLocaleString()} 行から型を推定しています。`
                + '　いちばん右の取得日時は取り込み時に自動で追加される列です。'),
            el('div', { id: 'previewTable' })),

        el('div', { class: 'card' },
            el('div', { class: 'card__title' }, '列の設定'),
            el('div', { id: 'planTable' })),

        el('div', { class: 'card' },
            el('div', { class: 'card__title' }, '取り込み先'),
            el('div', { class: 'row mb' },
                el('div', { style: 'width:240px' }, el('label', { class: 'field' }, '取り込み先のDB'), dbSelect),
                el('div', { id: 'newDbWrap', style: 'width:240px' },
                    el('label', { class: 'field' }, '新しいDBの名前'),
                    el('input', { type: 'text', id: 'dbName', value: r.suggest_db })),
                el('div', { style: 'width:240px' },
                    el('label', { class: 'field' }, 'テーブル名'),
                    el('input', { type: 'text', id: 'tableName', value: r.suggest_table }))),
            el('div', { class: 'row mb' },
                el('div', { style: 'width:320px' },
                    el('label', { class: 'field' }, '更新のしかた'),
                    el('select', { id: 'mode', onchange: syncMode },
                        Object.entries(IMP.modes).map(([k, v]) =>
                            el('option', { value: k }, v)))),
                el('div', { style: 'width:240px' },
                    el('label', { class: 'field' },
                        '取得日時の列名 ', el('span', { class: 'badge badge--err' }, '必須')),
                    el('input', { type: 'text', id: 'tsCol', value: IMP.defaultTs,
                        oninput: renderColumnPlan })),
                el('div', { id: 'keepWrap', class: 'hidden', style: 'width:220px' },
                    el('label', { class: 'field' },
                        '保存回数 ', el('span', { class: 'badge badge--err' }, '必須')),
                    el('input', { type: 'number', id: 'keepRuns', value: '',
                        placeholder: `1〜${IMP.maxKeep}`,
                        min: '1', max: String(IMP.maxKeep) }))),
            el('div', { class: 'small muted mb' },
                '取得日時の列は更新の仕方によらず必ず追加され、取り込んだ日時が入ります。'),
            el('div', { id: 'appendNote', class: 'hidden small muted mb' },
                `追記では、取り込み1回ぶんを「1回」と数え、新しい方から最大 ${IMP.maxKeep} 回分まで保持できます。`
                + '上限を超えた古い回は取り込みのたびに自動で削除されます'
                + '（取得日時が入っていない既存の行は消しません）。'),
            el('div', { id: 'destNote' }),
            el('div', { class: 'row mt' },
                el('button', { class: 'btn btn--primary', id: 'runImport' }, 'いま取り込む'),
                el('div', { class: 'spacer' }))),

        el('details', { class: 'acc' },
            el('summary', {}, 'この設定を定期取り込みに登録する'),
            el('div', { class: 'acc__body' },
                source?.kind === 'upload'
                    ? el('div', { class: 'alert alert--warn' },
                        'アップロードしたファイルは定期取り込みに登録できません。'
                        + 'サーバ上に残らないため、次回以降読み直せないからです。'
                        + '繰り返し取り込むなら、取り込み元フォルダに置いてから選び直してください。')
                    : null,
                el('div', { class: 'row' },
                    el('div', { class: 'grow' },
                        el('label', { class: 'field' }, '設定の名前'),
                        el('input', { type: 'text', id: 'jobName',
                            value: `${r.suggest_db} ${r.suggest_table}` })),
                    el('div', { style: 'width:210px' },
                        el('label', { class: 'field' }, '開始日時'),
                        el('input', { type: 'datetime-local', id: 'jobStart' })),
                    el('div', { style: 'width:170px' },
                        el('label', { class: 'field' }, '更新間隔'),
                        el('select', { id: 'jobInterval' },
                            IMP.intervals.map(i => el('option',
                                { ...(i === '1日ごと'? { selected: 'selected' } : {}) }, i)))),
                    el('button', { class: 'btn btn--sm', id: 'saveJob' }, '登録する')),
                el('div', { class: 'small muted mt' },
                    '開始日時を入れると、その時刻を過ぎるまで自動実行されません（空なら登録後すぐ対象）。'
                    + '「更新」での手動実行は開始日時に関係なくいつでもできます。'))));

    $('#dbTarget').addEventListener('change', syncDest);
    $('#tableName').addEventListener('input', syncDest);
    $('#runImport').addEventListener('click', runImport);
    $('#saveJob').addEventListener('click', saveJob);
    // 開始日時は過去を選べないようにする（分単位で今から）
    const jobStart = $('#jobStart');
    if (jobStart) jobStart.min = localNow();
    renderColumnPlan();
    syncMode(); syncDest();
}

/** datetime-local に入れる「今」。ローカル時刻の YYYY-MM-DDTHH:MM。 */
function localNow(offsetMinutes = 0) {
    const d = new Date(Date.now() + offsetMinutes * 60000);
    const p = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
         + `T${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 取得日時の列名。空なら既定値。 */
function tsName() {
    return ($('#tsCol')?.value || '').trim();
}

/** プレビューと「列の設定」を描き直す。取得日時の列も混ぜて見せる。 */
function renderColumnPlan() {
    const ts = tsName();
    const stamp = localNow().replace('T', ' ') + ':00';

    const pv = $('#previewTable');
    if (pv && previewInfo) {
        const cols = [...previewInfo.columns, ts ? `${ts}（自動追加）` : '（取得日時：列名が未入力）'];
        const rows = previewInfo.rows.slice(0, 30).map(r => [...r, ts ? stamp : '—']);
        pv.replaceChildren(dataTable(cols, rows));
    }

    const box = $('#planTable');
    if (!box) return;
    const rows = plan.map((c, i) => el('tr', {},
        el('td', {}, el('input', {
            type: 'checkbox', ...(c.include ? { checked: 'checked' } : {}),
            onchange: ev => { plan[i].include = ev.target.checked; },
        })),
        el('td', { class: 'muted' }, c.source),
        el('td', {}, el('input', {
            type: 'text', value: c.name,
            onchange: ev => { plan[i].name = ev.target.value; },
        })),
        el('td', {}, el('select', {
            onchange: ev => { plan[i].type = ev.target.value; },
        }, ['TEXT', 'INTEGER', 'REAL'].map(t =>
            el('option', { value: t, ...(t === c.type ? { selected: 'selected' } : {}) }, t))))));

    // 自動で足される取得日時の行。外せないので操作欄は出さない。
    rows.push(el('tr', { style: 'background:var(--accent-weak)' },
        el('td', {}, '自動'),
        el('td', { class: 'muted' }, '（取り込み日時）'),
        el('td', {}, ts
            ? el('b', {}, ts)
            : el('span', { style: 'color:var(--err)' }, '列名を入力してください')),
        el('td', {}, 'TEXT')));

    box.replaceChildren(el('div', { class: 'tablewrap', style: 'max-height:340px' },
        el('table', { class: 'data' },
            el('thead', {}, el('tr', {},
                el('th', { style: 'width:52px' }, '取込'), el('th', {}, '元の列名'),
                el('th', {}, '列名（DB側）'), el('th', { style: 'width:120px' }, '型'))),
            el('tbody', {}, rows))));
}

function syncMode() {
    const append = $('#mode').value === 'append';
    ['#keepWrap', '#appendNote'].forEach(s => $(s)?.classList.toggle('hidden', !append));
    syncDest();
}

/** 保存前の必須チェック。足りなければ理由を配列で返す。 */
function formProblems(forJob = false) {
    const out = [];
    if (!tsName()) out.push('取得日時の列名を入力してください（必須）。');
    if ($('#mode').value === 'append') {
        const raw = ($('#keepRuns')?.value || '').trim();
        const keep = parseInt(raw, 10);
        if (!raw) out.push('保存回数を入力してください（必須）。');
        else if (!Number.isInteger(keep) || keep < 1 || keep > IMP.maxKeep) {
            out.push(`保存回数は 1〜${IMP.maxKeep} で指定してください。`);
        }
    }
    if (forJob) {
        const raw = ($('#jobStart')?.value || '').trim();
        // input の min だけでは手入力を防げないので、送る前にもう一度見る
        if (raw && raw < localNow(-2)) {
            out.push(`開始日時に過去の時刻は指定できません（指定: ${raw.replace('T', ' ')}）。`);
        }
    }
    return out;
}

function syncDest() {
    const dbFile = $('#dbTarget').value;
    $('#newDbWrap').classList.toggle('hidden', !!dbFile);
    const note = $('#destNote');
    note.replaceChildren();
    const run = $('#runImport');
    if (run) { run.disabled = false; run.title = ''; }
    if (!dbFile) return;
    const tables = IMP.existing[dbFile] || [];
    const t = $('#tableName').value.trim();
    note.append(el('div', { class: 'small muted' },
        `このDBにあるテーブル: ${tables.length ? tables.join(', ') : '（なし）' }`));

    // 定期実行＋追記のテーブルは、ここから手で足しても間隔が崩れるので入れさせない
    const locked = (lockedTables[dbFile] || {})[t];
    if (locked) {
        note.append(el('div', { class: 'alert alert--warn mt' }, ''+ locked));
        if (run) { run.disabled = true; run.title = locked; }
        return;
    }
    if (tables.includes(t) && $('#mode').value === 'replace') {
        note.append(el('div', { class: 'alert alert--warn mt' },
            `${t} は既にあります。洗い替えなので、いま入っているデータは消えて入れ替わります。`));
    }
}

function importPayload() {
    const dbFile = $('#dbTarget').value;
    return {
        ...readOptions(),
        new_db: !dbFile, db_file: dbFile, db_name: $('#dbName')?.value,
        table: $('#tableName').value, mode: $('#mode').value,
        timestamp_column: $('#tsCol')?.value || null,
        keep_runs: $('#mode').value === 'append' ? $('#keepRuns')?.value : null,
        columns: plan,
    };
}

async function runImport(ev) {
    const bad = formProblems();
    if (bad.length) { bad.forEach(m => toast(m, 'warn')); return; }
    ev.target.disabled = true;
    ev.target.innerHTML = '<span class="spinner"></span> 取り込み中';
    try {
        const r = await api('/api/import/run', importPayload());
        let msg = `${r.db} の ${r.table} に ${r.rows.toLocaleString()}行を取り込みました。`;
        if (r.timestamp_column) msg += ` 取得日時列「${r.timestamp_column}」つき。`;
        if (r.keep) msg += ` 保持 ${r.kept}/${r.keep}回`;
        if (r.removed) msg += `（古い ${r.removed.toLocaleString()}行を削除）`;
        toast(msg, 'ok', 8000);
        if (r.degraded?.length) {
            toast(`数値にできない値があったため TEXT で取り込んだ列: ${r.degraded.join(', ')}`, 'warn', 9000);
        }
        setTimeout(() => window.location.reload(), 1500);
    } catch (e) { toast(e.message, 'err', 9000); }
    ev.target.disabled = false;
    ev.target.textContent = 'いま取り込む';
}

async function saveJob() {
    const bad = formProblems(true);
    if (bad.length) { bad.forEach(m => toast(m, 'warn')); return; }
    try {
        await api('/api/jobs/save', {
            ...importPayload(), name: $('#jobName').value, interval: $('#jobInterval').value,
            start_at: $('#jobStart').value,
        });
        toast('定期取り込みに登録しました。「DBの管理」タブで確認できます。');
        refreshManage();
    } catch (e) { toast(e.message, 'err', 9000); }
}

/* --- DBの管理（テーブルごとの詳細＋定期取り込み） -------------------------------- */

// 開いているテーブル（"DB名/表名"）。描き直しても開閉が戻らないように覚えておく。
const openTables = new Set();
// 手で更新してはいけないテーブル {DBファイル: {テーブル: 理由}}。管理タブを読むたび更新する。
let lockedTables = {};

function renderSched(s) {
    const box = $('#schedBanner');
    if (!s.enabled) {
        box.replaceChildren(el('div', { class: 'alert alert--warn' },
            '自動実行は停止しています（.env の IMPORT_SCHEDULER=false）。手動更新はできます。'));
    } else if (!s.running) {
        box.replaceChildren(el('div', { class: 'alert alert--err' },
            '自動実行のスレッドが動いていません。アプリを再起動してください。'));
    } else {
        box.replaceChildren(el('div', { class: 'alert alert--ok' },
            `自動実行中です（${s.tick_sec}秒ごとに確認 / 最終確認 ${(s.last_tick || '―').replace('T', ' ')}）。`
            + 'アプリが起動している間、画面を開いていなくても動きます。'));
    }
}

/** 定期取り込みの操作ボタン（頻度の変更・手動実行・停止・削除）。 */
function jobControls(j) {
    return [
        el('select', {
            style: 'width:130px',
            title: '更新の頻度',
            onchange: async ev => {
                await api('/api/jobs/update', { id: j.id, interval: ev.target.value });
                toast(`「${j.name}」を ${ev.target.value} に変更しました。`);
                refreshManage();
            },
        }, IMP.intervals.map(i => el('option',
            { ...(i === j.interval_label ? { selected: 'selected' } : {}) }, i))),
        // 定期実行＋追記は手で走らせると間隔が崩れるので押せなくする
        j.manual_blocked
            ? el('button', { class: 'btn btn--sm', disabled: 'disabled',
                             title: j.manual_blocked }, '今すぐ更新（不可）')
            : el('button', {
                class: 'btn btn--sm',
                onclick: async ev => {
                    ev.target.innerHTML = '<span class="spinner"></span>';
                    try {
                        const r = await api('/api/jobs/run', { id: j.id });
                        r.results.forEach(x =>
                            toast(`${x.name}: ${x.message}`, x.ok ? 'ok' : 'err', 7000));
                    } catch (e) { toast(e.message, 'err', 9000); }
                    refreshManage();
                },
            }, '今すぐ更新'),
        el('button', {
            class: 'btn btn--sm',
            onclick: async () => {
                await api('/api/jobs/update', { id: j.id, enabled: j.enabled === false });
                refreshManage();
            },
        }, j.enabled === false ? '再開' : '停止'),
        el('button', {
            class: 'btn btn--sm btn--danger',
            title: '定期取り込みの設定だけを消します（表とデータは残ります）',
            onclick: async () => {
                if (!confirm(`定期取り込み「${j.name}」の設定を削除しますか？\n`
                    + '（テーブルと中のデータは残ります）')) return;
                await api('/api/jobs/delete', { id: j.id });
                toast('定期取り込みの設定を削除しました。');
                refreshManage();
            },
        }, '設定'),
    ];
}

/** 1件ぶんの定期取り込みの中身（取り込み元と更新のしかた）。 */
function jobDetail(j, withName) {
    const box = el('div', { style: 'margin-top:6px' });
    if (withName) {
        box.append(el('div', { style: 'font-weight:600;font-size:12.5px;margin-bottom:2px' },
            `${j.name}`,
            j.enabled === false ? el('span', { class: 'badge badge--warn' }, '停止中') : null));
    }
    box.append(
        kv('ファイル名', j.source_label ? j.source_label.split(/[\\/]/).pop() : '―', true),
        kv('フルパス', j.source),
        kv('シート', j.sheet || '（Excel以外）'),
        kv('区切り文字', j.delimiter === null || j.delimiter === undefined
            ? '自動判定' : JSON.stringify(j.delimiter)),
        kv('見出しの行', (Number(j.header_row || 0) + 1) + ' 行目'),
        kv('更新の方法', j.mode_label, true),
        kv('更新の頻度', j.interval_label, true),
        kv('開始日時', (j.start_at || '').replace('T', ' ') || '（すぐ対象）'),
        kv('次回予定', j.next_label, true),
        kv('前回実行', (j.last_run || '').replace('T', ' ')),
        kv('前回結果', j.last_status === 'ok'? `${j.last_message || '' }`
            : j.last_status === 'error'? `${j.last_message || '' }` : '―'),
        kv('状態', j.enabled === false ? '停止中' : '有効'));
    if (j.mode === 'append') box.append(kv('保存回数', `${j.keep_runs} 回まで`, true));
    if (j.manual_blocked) {
        box.append(el('div', { class: 'small muted mt' }, ''+ j.manual_blocked));
    }
    box.append(el('div', { class: 'row mt', style: 'gap:6px' }, ...jobControls(j)));
    return box;
}

function kv(label, value, strong) {
    return el('div', { style: 'display:flex;gap:8px;font-size:12.5px;padding:1px 0' },
        el('span', { class: 'muted', style: 'width:120px;flex:0 0 120px' }, label),
        el('span', { class: strong ? '': 'mono', style: strong ? 'font-weight:600': '' },
            value === null || value === undefined || value === '' ? '―' : String(value)));
}

/* --- 削除（テーブル / DB） --------------------------------------------------------
   消す前に「何が巻き添えになるか」を必ず見せる。カタログの説明・関連・例文・
   検算ルールはあちこちのDBに散っていて、画面を見ているだけでは分からないため。 */

function impactList(groups) {
    if (!groups.length) {
        return el('div', { class: 'small muted' }, '巻き添えになるものはありません。');
    }
    return el('div', {}, groups.map(g => el('details', { class: 'acc' },
        el('summary', {},
            el('strong', {}, g.label),
            el('span', { class: 'muted small' }, `${g.items.length}件`)),
        el('div', { class: 'acc__body' },
            g.items.map(it => el('div', { class: 'small', style: 'padding:1px 0' },
                el('span', { class: 'muted mono', style: 'margin-right:6px' }, it.db),
                it.text))))));
}

/** 削除の確認ダイアログ。opts で文言と実行内容を差し替える。 */
async function confirmDelete(opts) {
    let groups;
    try {
        groups = (await api(opts.impactUrl, undefined, 'GET')).groups;
    } catch (e) { return toast(e.message, 'err'); }

    // この画面には「ファイルを選ぶ」の .modal が最初から置いてある。
    // 取り違えないよう、こちらには id を付けておく
    const back = el('div', { class: 'modal', id: 'delModal' });
    const close = () => back.remove();
    back.addEventListener('click', ev => { if (ev.target === back) close(); });

    const dropJobs = el('input', { type: 'checkbox', checked: 'checked' });
    const jobCount = (groups.find(g => g.key === 'jobs')?.items || []).length;
    // 合言葉。DB削除のときだけ、ファイル名をそのまま打ってもらう
    const phrase = opts.phrase
        ? el('input', { type: 'text', style: 'width:100%',
                        placeholder: opts.phrase, autocomplete: 'off' })
        : null;

    const go = el('button', {
        class: 'btn btn--sm btn--danger',
        ...(phrase ? { disabled: 'disabled' } : {}),
        onclick: async () => {
            go.disabled = true;
            try {
                const r = await api(opts.url, {
                    ...opts.body,
                    ...(phrase ? { confirm: phrase.value.trim() } : {}),
                    drop_jobs: dropJobs.checked,
                });
                close();
                toast(opts.done(r));
                refreshManage();
            } catch (e) { toast(e.message, 'err', 9000); go.disabled = false; }
        },
    }, opts.action);
    phrase?.addEventListener('input',
        () => { go.disabled = phrase.value.trim() !== opts.phrase; });

    back.append(el('div', { class: 'modal__box' },
        el('div', { class: 'modal__head' },
            el('b', { class: 'grow' }, opts.title),
            el('button', { class: 'btn btn--sm btn--ghost', onclick: close },
                icon('x', 'icon--sm'))),
        el('div', { class: 'modal__body', style: 'padding:12px 14px' },
            el('div', { class: 'alert alert--err' }, opts.warning),
            el('div', { class: 'small muted', style: 'margin:10px 0 4px' },
                '一緒に片づけるもの'),
            impactList(groups),
            jobCount
                ? el('label', { class: 'row mt', style: 'align-items:center;gap:6px' },
                    dropJobs,
                    el('span', { class: 'small' },
                        `定期取り込みの設定 ${jobCount} 件も削除する`
                        + '（外すと、次の実行でまた取り込まれます）'))
                : null,
            phrase
                ? el('div', { class: 'mt' },
                    el('div', { class: 'small', style: 'margin-bottom:4px' },
                        `確認のため、`, el('b', { class: 'mono' }, opts.phrase),
                        ` をそのまま入力してください。`),
                    phrase)
                : null),
        el('div', { class: 'modal__foot row', style: 'align-items:center' },
            el('div', { class: 'spacer' }),
            el('button', { class: 'btn btn--sm', onclick: close }, 'やめる'),
            go)));
    document.body.append(back);
    (phrase || go).focus();
}

function tableCard(dbName, t) {
    const js = t.jobs || [];
    const j = js[0];
    const head = el('summary', {},
        el('strong', {}, t.name),
        el('span', { class: 'muted small' },
            `${(t.rows || 0).toLocaleString()}行 / ${t.column_count}列`),
        js.length
            ? el('span', { class: j.enabled === false ? 'badge badge--warn': 'badge badge--ok' },
                js.length > 1 ? `定期取り込み ${js.length}件`
                    : (j.enabled === false ? '定期取り込み（停止中）' : `定期取り込み ${j.interval_label}`))
            : el('span', { class: 'badge' }, '定期取り込みなし'),
        js.some(x => x.last_status === 'error')
            ? el('span', { class: 'badge badge--err' }, '前回失敗') : null);

    const body = el('div', { class: 'acc__body' });

    // 定期取り込み（取り込み元と更新のしかた）
    body.append(el('div', { style: 'font-weight:700;margin-bottom:4px' }, '定期取り込み'));
    if (js.length) {
        js.forEach(x => body.append(jobDetail(x, js.length >1)));
    } else {
        body.append(el('div', { class: 'small muted' },
            '設定されていません。取り込み元も分かりません'
            + '（手動で取り込んだか、外部で作られたテーブルです）。'
            + '「ファイルから取り込む」で取り込むときに登録できます。'));
    }

    // いま入っているデータ
    body.append(el('div', { style: 'font-weight:700;margin:10px 0 4px' }, '中身'),
        kv('行数', (t.rows || 0).toLocaleString(), true),
        kv('列数', t.column_count),
        kv('取得日時列', t.timestamp_column || '（なし）'),
        kv('保持している回数', t.runs === null ? '―' : `${t.runs} 回分`, true),
        kv('最新の取り込み', (t.latest || '').replace('T', ' ')),
        kv('最古の取り込み', (t.oldest || '').replace('T', ' ')),
        kv('列', t.columns.join(', ')));

    // サンプル行と更新履歴は開いたときに取りに行く（全テーブル分を先読みすると重い）
    const sampleBox = el('div', { class: 'mt' }, el('div', { class: 'small muted' }, '—'));
    const histBox = el('div', { class: 'mt' }, el('div', { class: 'small muted' }, '—'));
    body.append(
        el('div', { class: 'row mt', style: 'align-items:center' },
            el('div', { style: 'font-weight:700' }, 'サンプル行'),
            el('div', { class: 'spacer' }),
            el('button', {
                class: 'btn btn--sm btn--ghost',
                onclick: () => loadTableDetail(dbName, t.name, sampleBox, histBox, true),
            }, '読み直す')),
        sampleBox,
        el('div', { style: 'font-weight:700;margin-top:10px' }, '更新履歴'),
        histBox);

    body.append(el('div', { class: 'row mt' },
        el('div', { class: 'spacer' }),
        el('button', {
            class: 'btn btn--sm btn--danger',
            onclick: () => confirmDelete({
                title: `テーブルを削除: ${dbName} の ${t.name}`,
                warning: `${t.name} と、その中の ${(t.rows || 0).toLocaleString()}行 を削除します。`
                         + 'この操作は元に戻せません。',
                impactUrl: `/api/import/impact?db=${encodeURIComponent(dbName)}`
                           + `&table=${encodeURIComponent(t.name)}`,
                url: '/api/import/drop-table',
                body: { db: dbName, table: t.name },
                action: 'テーブルを削除する',
                done: () => `${t.name} を削除し、カタログの記述も片づけました。`,
            }),
        }, 'テーブルを削除')));

    // 「今すぐ更新」を押すと一覧を描き直すので、開いていた表は開いたままにする
    const key = `${dbName}/${t.name}`;
    const acc = el('details', {
        class: 'acc',
        ...(openTables.has(key) ? { open: 'open' } : {}),
        ontoggle: ev => {
            if (!ev.target.open) { openTables.delete(key); return; }
            openTables.add(key);
            loadTableDetail(dbName, t.name, sampleBox, histBox);
        },
    }, head, body);
    // 最初から開いている場合は toggle が飛ばないので、こちらから読みに行く
    if (openTables.has(key)) loadTableDetail(dbName, t.name, sampleBox, histBox);
    return acc;
}

/** テーブルのサンプル行と更新履歴を取ってきて流し込む。 */
async function loadTableDetail(dbName, table, sampleBox, histBox, force) {
    if (sampleBox.dataset.loaded && !force) return;
    sampleBox.dataset.loaded = '1';
    const wait = () => el('div', { class: 'small muted' },
        el('span', { class: 'spinner' }), '読み込み中...');
    sampleBox.replaceChildren(wait());
    histBox.replaceChildren(wait());
    let r;
    try {
        r = await api(`/api/import/table?db=${encodeURIComponent(dbName)}`
            + `&table=${encodeURIComponent(table)}`, undefined, 'GET');
    } catch (e) {
        sampleBox.dataset.loaded = '';
        sampleBox.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
        histBox.replaceChildren();
        return;
    }
    renderSample(sampleBox, r.sample);
    renderHistory(histBox, r.history, r.kinds);
}

function renderSample(box, s) {
    if (s.error) {
        box.replaceChildren(el('div', { class: 'alert alert--warn' }, s.error));
        return;
    }
    if (!s.rows.length) {
        box.replaceChildren(el('div', { class: 'small muted' }, 'まだ1行も入っていません。'));
        return;
    }
    box.replaceChildren(
        el('div', { class: 'small muted', style: 'margin-bottom:4px' },
            s.order_by
                ? `最大 ${s.limit} 行 ・ 「${s.order_by}」の新しい順（直近の取り込み分が上）`
                : `先頭 ${s.limit} 行 ・ 取得日時の列がないので入っている順`),
        dataTable(s.columns, s.rows));
}

function renderHistory(box, list, kinds) {
    if (!list || !list.length) {
        box.replaceChildren(el('div', { class: 'small muted' },
            'まだありません。この画面から取り込むと、ここに1回ぶんずつ残ります。'));
        return;
    }
    const ok = list.filter(h => h.ok).length;
    const rows = list.map(h => el('tr', {},
        el('td', { class: 'mono' }, (h.at || '').replace('T', ' ')),
        el('td', {}, h.ok
            ? el('span', { style: 'color:var(--ok)' }, '成功')
            : el('span', { style: 'color:var(--err)' }, '失敗')),
        el('td', {}, (kinds || {})[h.kind] || h.kind),
        el('td', {}, h.mode === 'append' ? '追記' : '洗い替え'),
        el('td', { class: 'num' }, h.ok ? (h.rows || 0).toLocaleString() : '―'),
        el('td', { class: 'num' }, h.removed ? `-${h.removed.toLocaleString()}` : ''),
        el('td', {}, h.kept === null || h.kept === undefined ? ''
            : `${h.kept}${h.keep ? '/'+ h.keep : '' }`),
        el('td', {}, h.seconds === null || h.seconds === undefined ? '' : `${h.seconds}秒`),
        el('td', {}, h.user || (h.kind === 'auto' ? '（自動）' : '')),
        el('td', { title: h.message }, h.message || '')));

    box.replaceChildren(
        el('div', { class: 'small muted', style: 'margin-bottom:4px' },
            `直近 ${list.length} 件（成功 ${ok} / 失敗 ${list.length - ok}）`),
        el('div', { class: 'tablewrap', style: 'max-height:320px' },
            el('table', { class: 'data' },
                el('thead', {}, el('tr', {},
                    ['日時', '結果', 'きっかけ', '方法', '行数', '削除', '保持', '所要', '実行者', 'メッセージ']
                        .map(h => el('th', {}, h)))),
                el('tbody', {}, rows))));
}

/** 対象のテーブルがまだ無い（または消された）定期取り込み。 */
function renderOrphans(list) {
    const box = $('#orphanJobs');
    box.replaceChildren();
    if (!list.length) return;
    const card = el('div', { class: 'card' },
        el('div', { class: 'card__title' }, '対象のテーブルがない定期取り込み'),
        el('div', { class: 'card__desc' },
            'まだ一度も実行されていないか、テーブルが削除された設定です。'
            + '実行すればテーブルが作られます。'));
    list.forEach(j => card.append(el('div', { class: 'acc__body' },
        el('div', { class: 'small mono muted' }, `${j.db_file} / ${j.table}`),
        jobDetail(j, true))));
    box.append(card);
}

function renderManage(m) {
    lockedTables = m.locked || {};
    if ($('#dbTarget')) syncDest();       // 取り込みタブを開いたままでも鍵が効くように
    renderSched(m.sched);
    const btn = $('#runDue');
    btn.textContent = `期限が来た${m.due ?? 0}件を今すぐ更新`;
    btn.disabled = !m.due;

    const box = $('#dbDetails');
    box.replaceChildren();
    if (!m.dbs.length) {
        box.append(el('div', { class: 'alert alert--info' },
            'まだDBがありません。「ファイルから取り込む」で作れます。'));
    }
    m.dbs.forEach(d => {
        const card = el('div', { class: 'card' },
            el('div', { class: 'row', style: 'align-items:baseline' },
                el('div', { class: 'card__title', style: 'margin:0' }, d.name),
                el('span', { class: 'muted small' },
                    `${d.tables.length}テーブル ・ ${((d.size || 0) / 1024).toLocaleString(
                        undefined, { maximumFractionDigits: 0 })} KB ・ 更新 ${d.mtime || '―' }`),
                el('div', { class: 'spacer' }),
                el('button', {
                    class: 'btn btn--sm btn--ghost hastip',
                    'data-tip': 'このDBをファイルごと削除します。'
                                + '&#10;元には戻せません。',
                    onclick: () => confirmDelete({
                        title: `DBを削除: ${d.name}`,
                        warning: `${d.name} をファイルごと削除します。`
                                 + `テーブル ${d.tables.length} 件と、このDBのカタログ`
                                 + '（説明・関連・用語・例文・検算ルール）がまとめて消えます。'
                                 + 'この操作は元に戻せません。',
                        impactUrl: `/api/import/impact?db=${encodeURIComponent(d.name)}`,
                        url: '/api/import/delete-db',
                        body: { db: d.name },
                        phrase: d.name,
                        action: 'このDBを削除する',
                        done: () => `${d.name} を削除しました。`,
                    }),
                }, 'DBを削除')));
        if (d.error) {
            card.append(el('div', { class: 'alert alert--err mt' }, d.error));
        } else if (!d.tables.length) {
            card.append(el('div', { class: 'small muted mt' }, '（テーブルなし）'));
        } else {
            d.tables.forEach(t => card.append(tableCard(d.name, t)));
        }
        box.append(card);
    });
    renderOrphans(m.orphans || []);
}

async function refreshManage() {
    renderManage(await api('/api/import/manage', undefined, 'GET'));
}

/* --- 起動 ------------------------------------------------------------------- */

document.addEventListener('DOMContentLoaded', () => {
    $$('.tab').forEach(tab => tab.addEventListener('click', () => {
        $$('.tab').forEach(t => t.classList.toggle('is-active', t === tab));
        $$('.tabpane').forEach(p => p.classList.toggle('is-active', p.id === `pane-${tab.dataset.pane}`));
        if (tab.dataset.pane === 'manage') refreshManage();
    }));

    loadDirs();
    wireDirs();
    $('#pickServer')?.addEventListener('click', () => openBrowser(null));
    $('#browserClose')?.addEventListener('click', closeBrowser);
    $('#browser')?.addEventListener('click', ev => {
        if (ev.target.id === 'browser') closeBrowser();   // 背景をクリックで閉じる
    });
    document.addEventListener('keydown', ev => {
        if (ev.key === 'Escape') closeBrowser();
    });
    $('#pickLocal')?.addEventListener('click', () => $('#localFile')?.click());
    $('#localFile')?.addEventListener('change', ev => {
        if (ev.target.files?.[0]) chooseLocalFile(ev.target.files[0]);
        ev.target.value = '';        // 同じファイルを選び直せるように
    });
    ['#sheet', '#delimiter', '#headerRow'].forEach(sel =>
        $(sel)?.addEventListener('change', loadPreview));
    $('#reload')?.addEventListener('click', loadPreview);

    renderManage(IMP.manage);
    $('#refreshTables').addEventListener('click', refreshManage);
    $('#runDue').addEventListener('click', async ev => {
        ev.target.disabled = true;
        const r = await api('/api/jobs/run', { all_due: true });
        r.results.forEach(x => toast(`${x.name}: ${x.message}`, x.ok ? 'ok': 'err', 7000));
        if (!r.results.length) toast('期限が来たジョブはありませんでした。', 'warn');
        refreshManage();
    });
});
