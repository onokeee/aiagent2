"""使うモデルの選択。

2段構えになっている。
  管理者 … 「モデル設定」画面で、選ばせる候補・既定・画像対応の判定を決める
  利用者 … チャット画面のプルダウンで、その候補から自分の1つを選ぶ

利用者が選んだモデルは prefs.py（ユーザーごとのファイル）に残るので、
ログアウトしても次に入ったときは同じモデルのまま。

候補の決まり方は 管理者の設定 > env の OPENAI_MODELS > APIの /models の順。
「そのモデルは画像を送れるか」も、ここで一元的に判断する。
"""
from __future__ import annotations

import threading
import time

import yaml

import config
import prefs

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "models": []}
_CACHE_SEC = 300


# =============================================================================
# 管理者が決める設定（data/model_settings.yaml）
#
# env を初期値として、このファイルの内容で上書きする。
# メール設定と同じ考え方で、env は「まだ画面で決めていないときの値」。
# =============================================================================

ADMIN_KEYS = ("models", "default", "vision", "prompt_inline_limit")

#: カタログのインライン上限として認める範囲。
#: 下限は「1DBぶんの詳細（実測で平均5.3K字）が入る」ことを目安にした。
#: 上限は、いちばん広いモデルでも文脈を食い尽くさないところで止める。
INLINE_LIMIT_MIN = 4_000
INLINE_LIMIT_MAX = 400_000


def _read_admin() -> dict:
    p = config.MODEL_SETTINGS_FILE
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"[models] 設定を読めませんでした: {p} ({e})")
        return {}
    return {k: v for k, v in data.items() if k in ADMIN_KEYS} \
        if isinstance(data, dict) else {}


def _write_admin(data: dict) -> None:
    p = config.MODEL_SETTINGS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        p.write_text(yaml.safe_dump({k: data[k] for k in ADMIN_KEYS if k in data},
                                    allow_unicode=True, sort_keys=False),
                     encoding="utf-8")


def _vision_keys() -> list[str]:
    ov = _read_admin().get("vision")
    keys = ov if isinstance(ov, list) else None
    return [str(k).strip().lower() for k in (keys or config.OPENAI_VISION_MODELS)
            if str(k).strip()]


def default_model() -> str:
    """未選択のユーザーが使うモデル。"""
    return str(_read_admin().get("default") or config.OPENAI_MODEL or "").strip()


def is_vision(model: str) -> bool:
    """画像を送れるモデルか。名前に手がかりが含まれるかで判断する。

    モデル名は環境によって違うので、完全一致ではなく部分一致にしている
    （「モデル設定」画面、または env の OPENAI_VISION_MODELS で調整できる）。
    """
    low = str(model or "").lower()
    return any(key in low for key in _vision_keys())


def prompt_inline_limit() -> int:
    """カタログをそのまま system prompt に入れる上限（文字数）。

    「モデル設定」画面で決めた値が最優先。無ければ env（config）の初期値。
    ここを超えると要約に落ち、AIに列名が渡らなくなる。
    """
    raw = _read_admin().get("prompt_inline_limit")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return config.PROMPT_INLINE_LIMIT_CHARS
    return max(INLINE_LIMIT_MIN, min(v, INLINE_LIMIT_MAX))


def context_window(model: str) -> tuple:
    """そのモデルが一度に読める量（トークン）と、それが確かな値かどうか。

    戻り値: (トークン数, 分かっているモデルか)
    名前は環境によって違うので、前方一致の長い方から当てる。
    """
    low = str(model or "").lower()
    for key in sorted(config.MODEL_CONTEXT_WINDOWS, key=len, reverse=True):
        if key in low:
            return config.MODEL_CONTEXT_WINDOWS[key], True
    return config.MODEL_CONTEXT_DEFAULT, False


def _from_api() -> list[str]:
    """APIに聞ける環境なら、使えるモデルの一覧を取ってくる。"""
    import llm
    if not llm.is_configured():
        return []
    now = time.time()
    with _lock:
        if _cache["models"] and now - _cache["at"] < _CACHE_SEC:
            return list(_cache["models"])
    try:
        got = sorted(m.id for m in llm.client().models.list().data)
    except Exception as e:
        print(f"[models] 一覧を取得できませんでした: {e}")
        got = []
    with _lock:
        _cache["at"], _cache["models"] = now, got
    return list(got)


def source() -> str:
    """候補がどこから来ているか。"admin" | "env" | "default"

    画面に出す文言を、実態とずれないようにするためのもの。
    """
    if [str(m).strip() for m in (_read_admin().get("models") or []) if str(m).strip()]:
        return "admin"
    return "env" if config.OPENAI_MODELS else "default"


def available(refresh: bool = False) -> list[str]:
    """チャット画面のプルダウンに出す候補。

    決まり方は 管理者の設定 > env の OPENAI_MODELS > 既定＋利用中のモデル。

    APIが返す一覧はここでは使わない。以前は最後の手段として使っていたが、
    それだと何も設定していないときに babbage-002 のような使えないモデルまで
    100件以上並び、「モデル設定」で絞ったつもりが効いていないように見えた。

    何も決めていないときは既定だけにしたいところだが、それだと以前の一覧から
    選んでいた人が黙って別のモデルに変わってしまう。決まるまでの間は、
    すでに誰かが選んでいるモデルも残す（画面で候補を決めれば、そちらが優先）。
    """
    if refresh:
        with _lock:
            _cache["at"] = 0.0
    admin = [str(m).strip() for m in (_read_admin().get("models") or []) if str(m).strip()]
    names = admin or list(config.OPENAI_MODELS) or sorted(users_by_model())
    # 既定のモデルは必ず候補に入れる（一覧に出てこないAPIもあるため）
    d = default_model()
    if d and d not in names:
        names.insert(0, d)
    return names


def users_by_model() -> dict:
    """いま誰がどのモデルを選んでいるか。{モデル名: [ユーザー名, ...]}

    候補から外すとその人は既定に戻る。外す前に影響が見えるようにするため、
    利用者のフォルダを読んで集める（読むだけで、何も書き換えない）。
    """
    out: dict = {}
    root = config.USER_META_DIR
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        p = d / "prefs.yaml"
        if not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        m = str((data or {}).get("model") or "").strip()
        if m:
            out.setdefault(m, []).append(d.name)
    return out


def catalog(refresh: bool = False) -> list[str]:
    """管理者が候補を選ぶときに見せる「選べる全部」。

    available() は絞り込んだ後の一覧なので、管理画面ではこちらを使う。
    """
    if refresh:
        with _lock:
            _cache["at"] = 0.0
    return sorted(set(list(config.OPENAI_MODELS) + _from_api()))


def current(user=None) -> str:
    """そのユーザーがいま使うモデル。

    選んでいても、管理者が候補から外していれば既定に戻す。
    外したモデルを使い続けられると、絞り込んだ意味がなくなるため。
    """
    if user:
        chosen = prefs.get_model(user)
        if chosen and chosen in available():
            return chosen
    return default_model()


def choose(user, model: str) -> str:
    """モデルを選ぶ。管理者が決めた候補の中からだけ。"""
    model = str(model or "").strip()
    if not model:
        raise ValueError("モデル名が空です。")
    allowed = available()
    if model not in allowed:
        raise ValueError(f"{model} は選べません。"
                         f"選べるのは {'、'.join(allowed) or '（候補なし）'} です。")
    prefs.set_model(user, model)
    who = getattr(user, "username", None) or user
    print(f"[models] {who} のモデルを {model} にしました")
    return model


def status(user: str | None = None, refresh: bool = False) -> dict:
    cur = current(user)
    names = available(refresh)
    return {
        "current": cur,
        "models": [{"id": m, "vision": is_vision(m)} for m in names],
        "vision": is_vision(cur),
        "from_env": bool(config.OPENAI_MODELS),
        "image_max_mb": config.IMAGE_MAX_MB,
        "image_max_count": config.IMAGE_MAX_COUNT,
    }


# --- 管理画面向け -----------------------------------------------------------

def admin_status(refresh: bool = False, scope: list[dict] | None = None) -> dict:
    """「モデル設定」画面に渡す内容。

    scope を渡すと、そのデータ範囲で「文脈をどれだけ使うか」も一緒に返す
    （上限を決めるのに、いまの実測値が要るため）。
    """
    ov = _read_admin()
    chosen = [str(m).strip() for m in (ov.get("models") or []) if str(m).strip()]
    names = chosen or list(config.OPENAI_MODELS)
    out = {
        "models": names,
        "default": default_model(),
        "vision": _vision_keys(),
        "catalog": catalog(refresh),
        "source": source(),
        "effective": available(),
        "in_use": users_by_model(),
        "from_env": not chosen,
        "env_models": list(config.OPENAI_MODELS),
        "env_default": config.OPENAI_MODEL,
        "settings_file": str(config.MODEL_SETTINGS_FILE),
        "llm_ready": _llm_ready(),
        "prompt_inline_limit": prompt_inline_limit(),
        "prompt_inline_limit_env": config.PROMPT_INLINE_LIMIT_CHARS,
        "limit_min": INLINE_LIMIT_MIN,
        "limit_max": INLINE_LIMIT_MAX,
    }
    if scope is not None:
        import llm
        base = llm.budget(scope, model=default_model(), admin=True)
        out["budget"] = base
        # 候補それぞれで、いまのカタログがどれだけ文脈を食うか
        out["per_model"] = []
        for m in (names or [default_model()]):
            ctx, known = context_window(m)
            out["per_model"].append({
                "id": m, "context": ctx, "context_known": known,
                "now_pct": round(base["now_tokens"] / ctx * 100, 1) if ctx else 0.0,
                "at_limit_pct": round(base["at_limit_tokens"] / ctx * 100, 1) if ctx else 0.0,
            })
    return out


def _llm_ready() -> bool:
    import llm
    return llm.is_configured()


def save_admin(data: dict, user: str | None = None) -> dict:
    """「モデル設定」画面からの保存。"""
    models = [str(m).strip() for m in (data.get("models") or []) if str(m).strip()]
    if not models:
        raise ValueError("選択できるモデルを1つ以上残してください。")
    if len(models) != len(set(models)):
        raise ValueError("同じモデルが重複しています。")
    for m in models:
        if len(m) > 120:
            raise ValueError(f"モデル名が長すぎます: {m[:40]}…")

    default = str(data.get("default") or "").strip() or models[0]
    if default not in models:
        raise ValueError(f"既定のモデル {default} が候補に入っていません。")

    vision = [str(v).strip().lower() for v in (data.get("vision") or []) if str(v).strip()]

    limit = data.get("prompt_inline_limit", prompt_inline_limit())
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError("カタログの上限は数字で指定してください。") from None
    if not (INLINE_LIMIT_MIN <= limit <= INLINE_LIMIT_MAX):
        raise ValueError(f"カタログの上限は {INLINE_LIMIT_MIN:,}〜{INLINE_LIMIT_MAX:,} 字の"
                         f"範囲で指定してください（いま {limit:,}）。")

    _write_admin({"models": models, "default": default, "vision": vision,
                  "prompt_inline_limit": limit})
    print(f"[models] モデル設定を更新しました（{user or '不明'}）: "
          f"候補{len(models)}件 / 既定={default} / 画像判定={len(vision)}件 / "
          f"カタログ上限={limit:,}字")
    return admin_status()
