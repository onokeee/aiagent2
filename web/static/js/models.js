/* モデル設定（管理者のみ）。
   チャットのプルダウンに出す候補・既定・画像判定キーワードを決める。 */

let state = {};

function chips(box, items, onRemove, empty, decorate) {
    box.replaceChildren();
    if (!items.length) {
        box.append(el('div', { class: 'small muted' }, empty));
        return;
    }
    box.append(el('div', { class: 'chips' }, items.map((v, i) =>
        el('span', { class: 'chip' }, decorate ? decorate(v) : v,
            el('button', {
                class: 'chip__x', title: '削除',
                onclick: () => { onRemove(i); render(); },
            }, '×')))));
}

/* サーバ側の is_vision と同じ判定。保存前に結果を見せるため。 */
const isVision = (name) =>
    (state.vision || []).some(k => String(name).toLowerCase().includes(k));

/* そのモデルを選んでいる利用者の数。候補から外す前に見せる。 */
const usersOf = (name) => ((state.in_use || {})[name] || []).length;

function render() {
    const models = state.models || [];

    chips($('#modelList'), models, i => {
        const removed = models[i];
        const n = usersOf(removed);
        if (n && !confirm(`${removed} は ${n} 人が選んでいます。`
            + '候補から外すと、その人たちは既定のモデルに戻ります。よろしいですか？')) return;
        models.splice(i, 1);
        if (state.default === removed) state.default = models[0] || '';
    }, '候補がありません。「一覧から選ぶ」で使わせたいモデルを選んでください。',
        v => `${v}${isVision(v) ? '　': '' }${usersOf(v) ? `（利用者${usersOf(v)}人）` : ''}`);

    // 既定は候補の中からしか選べない
    const sel = $('#defaultModel');
    sel.replaceChildren(...(models.length
        ? models.map(v => el('option',
            { value: v, ...(v === state.default ? { selected: 'selected' } : {}) }, v))
        : [el('option', { value: '' }, '（候補を追加してください）')]));

    chips($('#visionList'), state.vision || [],
        i => state.vision.splice(i, 1),
        '未設定です。画像はどのモデルでも送れない扱いになります。');

    // 判定結果をその場で見せる（保存してから気づくのを防ぐ）
    $('#visionPreview').replaceChildren(models.length
        ? el('div', { class: 'small muted' },
            'いまの判定: '+ models.map(m => `${m} ${isVision(m) ? '画像OK': '画像なし' }`).join('／ '))
        : '');

    const cat = state.catalog || [];
    $('#catalogList').replaceChildren(...cat.map(v => el('option', { value: v })));
    $('#catalogHint').textContent = cat.length
        ? `APIが返したモデルは ${cat.length} 件です。`
          + 'この中から「一覧から選ぶ」で選ぶか、一覧に無い名前は直接入力してください。'
        : 'APIから一覧を取得できていません。モデル名を直接入力してください。';

    renderBudget();

    const box = $('#banner');
    box.replaceChildren();
    if (!state.llm_ready) {
        box.append(el('div', { class: 'alert alert--warn' },
            'LLMが未設定です。env の OPENAI_* を設定するまで、'
            + 'モデル一覧の取得とチャットは動きません。'));
    }
    // いまチャットに何が出ているかを、実態のまま出す。
    // ここがずれていると「設定が効いていない」ように見える。
    const eff = state.effective || [];
    if (state.source === 'admin') {
        box.append(el('div', { class: 'alert alert--info' },
            `この画面の設定が効いています。チャットのプルダウンには `
            + `${eff.length} 件（${eff.join('、')}）が出ます。`));
    } else if (state.source === 'env') {
        box.append(el('div', { class: 'alert alert--info' },
            'いまは env の OPENAI_MODELS をそのまま使っています'
            + `（${eff.join('、')}）。ここで保存すると、以後はこの画面の内容が優先されます。`));
    } else {
        box.append(el('div', { class: 'alert alert--warn' },
            '候補をまだ決めていません。いまチャットに出るのは、既定のモデルと'
            + `すでに誰かが選んでいるモデルだけです（${eff.join('、') || '（未設定）'}）。`
            + '「一覧から選ぶ」で使わせたいモデルを決めてください。'));
    }
}

/* --- 一覧から選ぶ（チェックボックス） ---------------------------------------------
   126件を1件ずつ手入力させるのは現実的ではないので、取得した一覧から選ばせる。 */

function openPicker() {
    const cat = state.catalog || [];
    if (!cat.length) {
        toast('APIから一覧を取得できていません。「一覧を取得」を試すか、名前を直接入力してください。', 'warn');
        return;
    }
    const picked = new Set(state.models || []);
    const back = el('div', { class: 'modal' });
    const close = () => back.remove();
    back.addEventListener('click', ev => { if (ev.target === back) close(); });

    const body = el('div', { class: 'modal__body' });
    const count = el('b', {}, '');
    const sync = () => { count.textContent = `${picked.size} 件を選択中`; };
    const rows = cat.map(name => {
        const cb = el('input', { type: 'checkbox', ...(picked.has(name) ? { checked: 'checked' } : {}) });
        cb.addEventListener('change', () => {
            if (cb.checked) picked.add(name); else picked.delete(name);
            sync();
        });
        return el('label', { class: 'fsrow', 'data-key': name },
            cb,
            el('span', { class: 'name' }, name),
            isVision(name) ? el('span', { class: 'small muted' }, '画像OK') : null,
            usersOf(name) ? el('span', { class: 'small muted' }, `利用者${usersOf(name)}人`) : null);
    });
    body.append(...rows);
    sync();

    const filter = el('input', {
        type: 'text', style: 'width:100%', placeholder: 'モデル名で絞り込み（例: gpt-4o）',
        oninput: ev => {
            const q = ev.target.value.trim().toLowerCase();
            rows.forEach(r => r.classList.toggle('hidden',
                !!q && !r.dataset.key.toLowerCase().includes(q)));
        },
    });

    back.append(el('div', { class: 'modal__box' },
        el('div', { class: 'modal__head' },
            el('b', { class: 'grow' }, '使わせるモデルを選ぶ'),
            el('button', { class: 'btn btn--sm btn--ghost', onclick: close }, icon('x', 'icon--sm'))),
        el('div', { style: 'padding:10px 14px 0' }, filter,
            el('div', { class: 'small muted mt' },
                `APIが返した ${cat.length} 件です。チェックしたものだけがチャットに出ます。`)),
        body,
        el('div', { class: 'modal__foot row', style: 'align-items:center' },
            el('span', { class: 'small muted grow' }, count),
            el('button', { class: 'btn btn--sm', onclick: close }, 'やめる'),
            el('button', {
                class: 'btn btn--sm btn--primary',
                onclick: () => {
                    // 一覧に無いのに手入力で足した名前は消さずに残す
                    const kept = (state.models || []).filter(m => !cat.includes(m));
                    state.models = [...kept, ...cat.filter(m => picked.has(m))];
                    if (!state.models.includes(state.default)) state.default = state.models[0] || '';
                    close(); render();
                    toast('選びました。下の「設定を保存」で確定します。', 'warn');
                },
            }, 'この内容にする'))));
    document.body.append(back);
    filter.focus();
}

/* --- カタログの量と、モデルの文脈に対する余裕 ------------------------------------
   数字だけ出しても判断できないので、「いまどれだけ使っていて、上限まで育てたら
   どうなるか」を並べて見せる。トークン数は実測から出した概算（llm.py 参照）。 */

const fmt = (n) => Number(n || 0).toLocaleString();

function bar(usedPct, limitPct) {
    // いまの使用量（濃い）と、上限まで育ったときの使用量（薄い）を重ねる
    const w = (v) => `${Math.min(100, Math.max(0, v))}%`;
    return el('div', { class: 'ctxbar', title: `いま ${usedPct}% / 上限まで育つと ${limitPct}%` },
        el('div', { class: 'ctxbar__limit', style: `width:${w(limitPct)}` }),
        el('div', { class: 'ctxbar__now', style: `width:${w(usedPct)}` }));
}

function renderBudget() {
    const inp = $('#inlineLimit');
    if (state.prompt_inline_limit && inp.value === '') {
        inp.value = state.prompt_inline_limit;
    }
    inp.min = state.limit_min || 4000;
    inp.max = state.limit_max || 400000;

    const b = state.budget;
    const now = $('#limitNow');
    if (!b) {
        now.replaceChildren(el('div', { class: 'small muted' },
            '対象データを選ぶと、いまの使用量が出ます'
            + '（チャット画面のサイドバーで選択してください）。'));
        $('#limitBar').replaceChildren();
        $('#modelBudget').replaceChildren();
        return;
    }

    const limit = parseInt(inp.value || b.limit_chars, 10) || b.limit_chars;
    const over = b.catalog_chars > limit;
    now.replaceChildren(
        el('div', { class: 'small' },
            `全DBを選んだ場合のカタログ: ${fmt(b.catalog_chars)} 字`),
        el('div', { class: `small ${over ? 'ctxwarn' : 'muted'}` },
            over
                ? `上限 ${fmt(limit)} 字を超えるので要約版になります（列名が渡りません）。`
                : `上限 ${fmt(limit)} 字まで、あと ${fmt(limit - b.catalog_chars)} 字ぶん`
                  + `説明を書き足せます。`));

    // 入力された上限で計算し直す（サーバと同じ式。保存前に結果が見える）
    const atLimitPct = (context) => {
        const tok = b.base_tokens + limit * b.tokens_per_char;
        return context ? Math.round(tok / context * 1000) / 10 : 0;
    };
    const atPct = atLimitPct(b.context);

    // 既定モデルでの帯
    $('#limitBar').replaceChildren(
        el('div', { class: 'small muted mb' },
            `既定モデル ${b.model}（一度に読める量 ${fmt(b.context)} トークン`
            + `${b.context_known ? '' : '・推定'}）に対して`),
        bar(b.now_pct, atPct),
        el('div', { class: `small ${atPct >= 70 ? 'ctxwarn' : 'muted'} mt` },
            `いま ${b.now_pct}% を使用（残り ${b.headroom_pct}%）。`
            + `上限まで育つと ${atPct}%。`
            + (atPct >= 70
                ? '会話の履歴に使える余地が少なくなります。上限を下げるか、'
                  + '文脈の広いモデルを既定にしてください。'
                : '残りは会話の履歴と回答に使われます。')));

    // 候補モデルごとの比較（同じカタログでも、モデルによって余裕が全然違う）
    const rows = (state.per_model || []).map(m => [
        m.id + (m.context_known ? '' : '（推定）'),
        `${fmt(m.context)} tok`,
        `${m.now_pct}%`,
        `${atLimitPct(m.context)}%`,
        atLimitPct(m.context) >= 70 ? '⚠ 余裕が少ない' : '',
    ]);
    $('#modelBudget').replaceChildren(rows.length
        ? el('div', {},
            el('div', { class: 'small muted mb' }, 'モデルごとの見込み'),
            dataTable(['モデル', '一度に読める量', 'いまの使用', '上限まで育つと', ''], rows))
        : '');
}

function addTo(key, input, normalize) {
    const v = (input.value || '').trim();
    if (!v) return;
    const value = normalize ? normalize(v) : v;
    state[key] = state[key] || [];
    if (state[key].some(x => x.toLowerCase() === value.toLowerCase())) {
        toast('すでに登録されています。', 'warn');
        return;
    }
    state[key].push(value);
    input.value = '';
    if (key === 'models'&& !state.default) state.default = value;
    render();
}

async function load(refresh) {
    state = await api(`/api/models/admin${refresh ? '?refresh=1': '' }`, undefined, 'GET');
    $('#inlineLimit').value = state.prompt_inline_limit || '';
    render();
}

document.addEventListener('DOMContentLoaded', () => {
    state = window.MODELS || {};
    render();

    $('#pickModel').addEventListener('click', openPicker);
    $('#addModel').addEventListener('click', () => addTo('models', $('#newModel')));
    $('#addVision').addEventListener('click',
        () => addTo('vision', $('#newVision'), v => v.toLowerCase()));
    [['#newModel', '#addModel'], ['#newVision', '#addVision']].forEach(([i, b]) =>
        $(i).addEventListener('keydown', ev => { if (ev.key === 'Enter') $(b).click(); }));

    $('#defaultModel').addEventListener('change', ev => { state.default = ev.target.value; });
    // 入力しながら結果が見えるようにする（保存してから気づくのを防ぐ）
    $('#inlineLimit').addEventListener('input', renderBudget);

    $('#refresh').addEventListener('click', async ev => {
        ev.target.disabled = true;
        try { await load(true); toast('一覧を取り直しました。', 'ok'); }
        catch (e) { toast(e.message, 'err', 9000); }
        ev.target.disabled = false;
    });

    $('#save').addEventListener('click', async ev => {
        ev.target.disabled = true;
        try {
            const r = await api('/api/models/admin', {
                models: state.models || [],
                default: state.default,
                vision: state.vision || [],
                prompt_inline_limit: parseInt($('#inlineLimit').value || '0', 10)
                                     || state.prompt_inline_limit,
            });
            state = { ...state, ...r };
            $('#inlineLimit').value = state.prompt_inline_limit;
            toast('保存しました。', 'ok');
            render();
        } catch (e) { toast(e.message, 'err', 9000); }
        ev.target.disabled = false;
    });

    $('#reload').addEventListener('click', () => load(false));
});
