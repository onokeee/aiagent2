/* テーブル全体を見る画面。

   サンプル行の「テーブル全体を閲覧」から別タブで開く読み取り専用のビューア。
   行はサーバ側で1ページずつ切って返るので、何百万行あっても画面は重くならない。
   絞り込み・並べ替えもサーバ側（表示中のページだけを並べても意味がないため）。 */

const T = window.TABLE_INIT || {};
let state = { offset: 0, limit: 100, q: '', sort: '', dir: 'asc', total: 0, matched: 0 };
let timer = null;

async function load() {
    const box = $('#tableBox');
    box.replaceChildren(el('div', { class: 'small muted', style: 'padding:10px' },
        el('span', { class: 'spinner' }), ' 読み込み中…'));
    const p = new URLSearchParams({
        db: T.db, table: T.table, offset: state.offset, limit: state.limit,
        q: state.q, sort: state.sort, dir: state.dir,
    });
    let r;
    try {
        r = await api('/api/table/rows?' + p.toString(), undefined, 'GET');
    } catch (e) {
        box.replaceChildren(el('div', { class: 'alert alert--err' }, e.message));
        return;
    }
    Object.assign(state, {
        total: r.total, matched: r.matched, offset: r.offset,
        limit: r.limit, sort: r.sort, dir: r.dir,
    });
    render(r);
}

function render(r) {
    // 見出しは押すと並べ替え。いまの並び順は ↑↓ で示す
    const head = el('thead', {}, el('tr', {},
        el('th', { style: 'text-align:right;width:1%' }, '#'),
        r.columns.map(c => el('th', {
            style: 'cursor:pointer;user-select:none',
            title: `${c} で並べ替え`,
            onclick: () => {
                if (state.sort === c) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
                else { state.sort = c; state.dir = 'asc'; }
                state.offset = 0; load();
            },
        }, c, state.sort === c ? el('span', { class: 'muted' }, state.dir === 'asc' ? ' ↑' : ' ↓') : null))));

    const body = el('tbody', {}, r.rows.map((row, i) => el('tr', {},
        el('td', { class: 'num muted' }, (r.offset + i + 1).toLocaleString()),
        row.map(v => {
            const info = cellInfo(v);
            return el('td', {
                class: info.num ? 'num' : null,
                title: info.text,
                // 値が NULL なのか空文字なのかは、集計の食い違いの原因になるので区別して出す
                ...(v === null || v === undefined ? { class: 'muted' } : {}),
            }, v === null || v === undefined ? 'NULL' : info.text);
        }))));

    $('#tableBox').replaceChildren(el('table', { class: 'data' }, head, body));

    const shown = r.rows.length;
    $('#countLabel').textContent = state.q
        ? `全 ${state.total.toLocaleString()}行 中 ${state.matched.toLocaleString()}行が一致`
          + `（${shown ? (r.offset + 1).toLocaleString() + '〜' + (r.offset + shown).toLocaleString() : '0'}行目を表示）`
        : `全 ${state.total.toLocaleString()}行 ・ ${r.columns.length}列`
          + `（${shown ? (r.offset + 1).toLocaleString() + '〜' + (r.offset + shown).toLocaleString() : '0'}行目を表示）`;

    const end = Math.max(1, Math.ceil((state.matched || 0) / state.limit));
    const page = Math.floor(state.offset / state.limit) + 1;
    $('#pageLabel').textContent = `${page} / ${end} ページ`;
    $('#prev').disabled = $('#first').disabled = state.offset <= 0;
    $('#next').disabled = $('#last').disabled = state.offset + state.limit >= state.matched;
}

function move(delta) {
    state.offset = Math.max(0, state.offset + delta * state.limit);
    load();
}

document.addEventListener('DOMContentLoaded', () => {
    if (T.error) return;                       // テーブルが無いときは表示だけ
    $('#q').addEventListener('input', ev => {
        // 1文字ごとに投げない（打ち終わりを待つ）
        clearTimeout(timer);
        timer = setTimeout(() => { state.q = ev.target.value.trim(); state.offset = 0; load(); }, 300);
    });
    $('#size').addEventListener('change', ev => {
        state.limit = Number(ev.target.value); state.offset = 0; load();
    });
    $('#first').addEventListener('click', () => { state.offset = 0; load(); });
    $('#prev').addEventListener('click', () => move(-1));
    $('#next').addEventListener('click', () => move(1));
    $('#last').addEventListener('click', () => {
        state.offset = Math.max(0, (Math.ceil(state.matched / state.limit) - 1) * state.limit);
        load();
    });
    load();
});
