import hashlib
import io
import json
import zipfile
from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from google import genai
from google.genai import types
from PIL import Image, ImageOps

try:
    import stripe
except ImportError:
    stripe = None

st.set_page_config(
    page_title="撮るだけ確定申告 Pro",
    page_icon="🧾",
    layout="centered",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    [data-testid="stHeader"],
    [data-testid="stToolbar"],
    #MainMenu,
    footer {
        display: none !important;
    }
    .block-container {
        padding-top: 1.2rem !important;
        padding-bottom: 1.5rem !important;
    }
    /* タイトル以外（本文・ラベル・ボタン・キャプション・タブ）を少し大きくして読みやすくする */
    .stMarkdown p,
    .stCaption,
    .stAlert,
    label,
    .stButton > button,
    .stLinkButton > a,
    .stDownloadButton > button,
    [data-baseweb="tab"] {
        font-size: 1.05rem !important;
    }
    /* 「撮影」ボタンの中身を消して「撮影」だけ表示 */
    [data-testid="stFileUploader"] button[data-pro-btn="camera"] {
        font-size: 0 !important;
        line-height: 0 !important;
    }
    [data-testid="stFileUploader"] button[data-pro-btn="camera"] > *,
    [data-testid="stFileUploader"] button[data-pro-btn="camera"] svg,
    [data-testid="stFileUploader"] button[data-pro-btn="camera"] span {
        display: none !important;
    }
    [data-testid="stFileUploader"] button[data-pro-btn="camera"]::after {
        content: "撮影";
        display: inline-block;
        font-size: 0.95rem !important;
        line-height: 1.4 !important;
    }
    /* 「画像・PDFを選ぶ」ボタンの中身を消して「ファイル選択」だけ表示 */
    [data-testid="stFileUploader"] button[data-pro-btn="upload"] {
        font-size: 0 !important;
        line-height: 0 !important;
    }
    [data-testid="stFileUploader"] button[data-pro-btn="upload"] > *,
    [data-testid="stFileUploader"] button[data-pro-btn="upload"] svg,
    [data-testid="stFileUploader"] button[data-pro-btn="upload"] span {
        display: none !important;
    }
    [data-testid="stFileUploader"] button[data-pro-btn="upload"]::after {
        content: "ファイル選択";
        display: inline-block;
        font-size: 0.95rem !important;
        line-height: 1.4 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

PRIMARY_MODEL = "gemini-3.1-flash-lite"
FREE_TRIAL_LIMIT = 10
WARN_LIMIT_TEXT_ONLY = 300
WARN_LIMIT_WITH_IMAGES = 150

ACCOUNT_CATEGORIES = [
    "消耗品費", "旅費交通費", "接待交際費", "会議費", "通信費",
    "水道光熱費", "地代家賃", "新聞図書費", "広告宣伝費", "車両費",
    "支払手数料", "外注工賃", "修繕費", "租税公課", "雑費",
]


def get_secret(key: str, fallback: str = "") -> str:
    val = st.secrets.get(key, "")
    if not val and fallback:
        val = st.secrets.get(fallback, "")
    return str(val).strip()


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def get_monitor_emails() -> set[str]:
    raw_items = []
    for k in ("MONITOR_EMAILS", "ALLOWLIST_EMAILS", "TEST_EMAILS"):
        v = str(st.secrets.get(k, "")).strip()
        if v:
            raw_items.append(v)
    emails = set()
    for chunk in raw_items:
        for part in chunk.replace("\n", ",").split(","):
            em = normalize_email(part)
            if em:
                emails.add(em)
    return emails


def get_valid_passcodes() -> list[str]:
    raw_items = []
    for k in ("PRO_PASSCODES", "PRO_PASSCODE", "ACCESS_CODE"):
        v = str(st.secrets.get(k, "")).strip()
        if v:
            raw_items.append(v)
    codes = []
    for chunk in raw_items:
        for part in chunk.replace("\n", ",").split(","):
            c = part.strip()
            if c and c not in codes:
                codes.append(c)
    return codes


def email_token(email: str) -> str:
    return hashlib.sha256(f"kakutei_pro_email_v1:{normalize_email(email)}".encode("utf-8")).hexdigest()


def init_session():
    defaults = {
        "is_pro": False,
        "pro_email": "",
        "pro_plan_label": "",
        "records": [],
        "saved_images": {},
        "keep_images": False,
        "processed_hashes": set(),
        "last_status": None,
        "zip_bytes": None,
        "zip_name": "",
        "excel_bytes": None,
        "excel_name": "",
        "export_dirty": False,
        "cam_key": 0,
        "up_key": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def verify_email_subscription(email: str) -> tuple[bool, str, str]:
    """
    入力されたメールアドレスがモニター許可リストまたはStripeの有効な定期購読に該当するか確認する。
    戻り値: (is_valid, plan_label, message)
    """
    em = normalize_email(email)
    if not em or "@" not in em:
        return False, "", "有効なメールアドレスを入力してください。"

    if em in get_monitor_emails():
        return True, "モニター／テスト枠", f"モニター許可リストで確認しました（{em}）。"

    api_key = get_secret("STRIPE_SECRET_KEY")
    if not api_key:
        return False, "", "現在、定期購読の自動照合キーが設定されていません。モニター許可リストまたはパスコードをご確認ください。"

    if stripe is None:
        return False, "", "stripe ライブラリがインストールされていません。requirements.txt に stripe を追加してください。"

    try:
        stripe.api_key = api_key
        customers = stripe.Customer.list(email=em, limit=20)
        customer_data = getattr(customers, "data", []) or []

        for cust in customer_data:
            cust_id = cust.get("id") if isinstance(cust, dict) else getattr(cust, "id", None)
            if not cust_id:
                continue
            subs = stripe.Subscription.list(customer=cust_id, status="all", limit=20)
            sub_data = getattr(subs, "data", []) or []
            for sub in sub_data:
                status = sub.get("status") if isinstance(sub, dict) else getattr(sub, "status", "")
                if status in ("active", "trialing"):
                    label = "Pro版有効中（年額定期購読）" if status == "active" else "Pro版有効中（トライアル中）"
                    return True, label, f"Stripeの有効な契約を確認しました（{em}）。"

        return False, "", "このメールアドレスで有効な定期購読（active）が見つかりませんでした。"
    except Exception as exc:
        return False, "", f"Stripe照合中にエラーが発生しました: {exc}"


def save_pro_session(email: str, plan_label: str):
    em = normalize_email(email)
    token = email_token(em)
    st.session_state["is_pro"] = True
    st.session_state["pro_email"] = em
    st.session_state["pro_plan_label"] = plan_label

    try:
        st.query_params["pro_user"] = token
    except Exception:
        pass

    payload = json.dumps({"email": em, "token": token})
    components.html(
        f"""
        <script>
        (function() {{
            const rawPayload = {json.dumps(payload)};
            try {{
                localStorage.setItem("kakutei_pro_auth", rawPayload);
                if (window.parent && window.parent.localStorage) {{
                    window.parent.localStorage.setItem("kakutei_pro_auth", rawPayload);
                }}
            }} catch (e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


def clear_pro_session(reset_browser: bool = True):
    st.session_state["is_pro"] = False
    st.session_state["pro_email"] = ""
    st.session_state["pro_plan_label"] = ""

    if not reset_browser:
        return

    try:
        for k in ("pro", "code", "pro_user", "email"):
            if k in st.query_params:
                del st.query_params[k]
    except Exception:
        pass

    components.html(
        """
        <script>
        (function() {
            try {
                localStorage.removeItem("kakutei_pro_auth");
                localStorage.removeItem("kakutei_pro_token");
                if (window.parent && window.parent.localStorage) {
                    window.parent.localStorage.removeItem("kakutei_pro_auth");
                    window.parent.localStorage.removeItem("kakutei_pro_token");
                }
                const parentWin = window.parent || window;
                const url = new URL(parentWin.location.href);
                let changed = false;
                ["pro", "code", "pro_user", "email"].forEach(function(param) {
                    if (url.searchParams.has(param)) {
                        url.searchParams.delete(param);
                        changed = true;
                    }
                });
                if (changed) {
                    parentWin.history.replaceState(null, "", url.toString());
                }
            } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )


def sync_pro_status():
    if st.session_state.get("is_pro") and st.session_state.get("pro_email"):
        ok, label, _ = verify_email_subscription(st.session_state["pro_email"])
        if ok:
            st.session_state["pro_plan_label"] = label
        else:
            clear_pro_session(reset_browser=True)
            st.session_state["last_status"] = (
                "warning",
                "定期購読の有効期限切れ・解約またはモニター対象外となったため、無料お試し枠に戻りました。",
            )
        return

    query_email = normalize_email(st.query_params.get("email", ""))
    if query_email:
        ok, label, _ = verify_email_subscription(query_email)
        if ok:
            save_pro_session(query_email, label)
            return

    components.html(
        """
        <script>
        (function() {
            try {
                const raw = localStorage.getItem("kakutei_pro_auth") ||
                    (window.parent && window.parent.localStorage ? window.parent.localStorage.getItem("kakutei_pro_auth") : "");
                if (!raw) return;
                const parsed = JSON.parse(raw);
                if (parsed && parsed.email) {
                    const parentWin = window.parent || window;
                    const url = new URL(parentWin.location.href);
                    if (url.searchParams.get("email") !== parsed.email) {
                        url.searchParams.set("email", parsed.email);
                        parentWin.history.replaceState(null, "", url.toString());
                        parentWin.location.reload();
                    }
                }
            } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )


init_session()
sync_pro_status()


def apply_camera_capture_preference():
    components.html(
        """
        <script>
        (function() {
            function bindCaptureInputs() {
                const doc = (window.parent && window.parent.document) || document;
                const uploaders = doc.querySelectorAll('[data-testid="stFileUploader"]');
                uploaders.forEach(function(box) {
                    const txt = box.innerText || "";
                    const inp = box.querySelector('input[type="file"]');
                    const btn = box.querySelector('button');
                    if (txt.includes("撮影")) {
                        if (inp) {
                            inp.setAttribute("accept", "image/*");
                            inp.setAttribute("capture", "environment");
                        }
                        if (btn && btn.getAttribute("data-pro-btn") !== "camera") {
                            btn.setAttribute("data-pro-btn", "camera");
                        }
                    } else if (txt.includes("画像・PDFを選ぶ")) {
                        if (inp) {
                            inp.removeAttribute("capture");
                            inp.setAttribute("accept", "image/*,application/pdf,.heic");
                        }
                        if (btn && btn.getAttribute("data-pro-btn") !== "upload") {
                            btn.setAttribute("data-pro-btn", "upload");
                        }
                    }
                });
            }
            bindCaptureInputs();
            [100, 300, 700, 1500].forEach(function(ms) {
                setTimeout(bindCaptureInputs, ms);
            });
            try {
                const doc = (window.parent && window.parent.document) || document;
                if (doc && doc.body && !doc.body._kakuteiBtnObserver) {
                    doc.body._kakuteiBtnObserver = true;
                    const obs = new MutationObserver(function() {
                        bindCaptureInputs();
                    });
                    obs.observe(doc.body, { childList: true, subtree: true });
                }
            } catch (e) {}
        })();
        </script>
        """,
        height=0,
    )


def optimize_image(file_bytes: bytes, max_px: int = 1200, quality: int = 72) -> bytes:
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_px:
            scale = max_px / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    except Exception:
        return file_bytes


def build_excel_bytes(records: list) -> bytes:
    cols = [
        "日付", "取引先・店名", "勘定科目", "税込金額", "税率(%)",
        "消費税額", "インボイス番号", "支払方法", "摘要・品目", "画像ファイル名",
    ]
    df = pd.DataFrame(records)
    for col in cols:
        if col not in df.columns:
            df[col] = ""
    df = df[cols]

    if not df.empty:
        df["税込金額"] = pd.to_numeric(df["税込金額"], errors="coerce").fillna(0).astype(int)

    summary = (
        df.groupby("勘定科目", as_index=False)["税込金額"]
        .sum()
        .sort_values(by="税込金額", ascending=False)
        if not df.empty
        else pd.DataFrame(columns=["勘定科目", "税込金額"])
    )

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="経費明細一覧")
        summary.to_excel(writer, index=False, sheet_name="勘定科目別集計")
    return out.getvalue()


def rebuild_export_files():
    records = st.session_state["records"]
    if not records:
        st.session_state["excel_bytes"] = None
        st.session_state["excel_name"] = ""
        st.session_state["zip_bytes"] = None
        st.session_state["zip_name"] = ""
        st.session_state["export_dirty"] = False
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    excel_name = f"kakutei_shinkoku_{stamp}.xlsx"
    excel_bytes = build_excel_bytes(records)
    st.session_state["excel_bytes"] = excel_bytes
    st.session_state["excel_name"] = excel_name

    if st.session_state["keep_images"] and st.session_state["saved_images"]:
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr(excel_name, excel_bytes)
            for fname, img_bytes in st.session_state["saved_images"].items():
                zf.writestr(f"receipts/{fname}", img_bytes)
        st.session_state["zip_bytes"] = zbuf.getvalue()
        st.session_state["zip_name"] = f"kakutei_set_{stamp}.zip"
    else:
        st.session_state["zip_bytes"] = None
        st.session_state["zip_name"] = ""

    st.session_state["export_dirty"] = False


def analyze_receipt(data_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    api_key = get_secret("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が未設定です。")

    client = genai.Client(api_key=api_key)
    prompt = (
        "レシート・領収書・請求書から項目を抽出し、JSONのみで返してください。\n"
        "- date: 日付（YYYY-MM-DD形式。不明時は今日の日付）\n"
        "- store: 取引先・店舗名\n"
        f"- category: 勘定科目（次から1つ選択: {', '.join(ACCOUNT_CATEGORIES)}）\n"
        "- amount: 税込合計金額（整数）\n"
        "- tax_rate: 主な消費税率（10 または 8 または 0）\n"
        "- tax_amount: 消費税額（整数。記載なければ税込金額から逆算）\n"
        "- invoice_no: インボイス登録番号（Tから始まる13桁。なければ空文字）\n"
        "- payment_method: 支払方法（現金 / クレジットカード / 電子マネー・QR / 振込 / 不明）\n"
        "- summary: 主な品目・用途の簡潔な説明"
    )

    schema = {
        "type": "OBJECT",
        "properties": {
            "date": {"type": "STRING"},
            "store": {"type": "STRING"},
            "category": {"type": "STRING"},
            "amount": {"type": "INTEGER"},
            "tax_rate": {"type": "INTEGER"},
            "tax_amount": {"type": "INTEGER"},
            "invoice_no": {"type": "STRING"},
            "payment_method": {"type": "STRING"},
            "summary": {"type": "STRING"},
        },
        "required": [
            "date", "store", "category", "amount", "tax_rate",
            "tax_amount", "invoice_no", "payment_method", "summary",
        ],
    }

    res = client.models.generate_content(
        model=PRIMARY_MODEL,
        contents=[types.Part.from_bytes(data=data_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.1,
        ),
    )
    raw_text = (res.text or "").strip()
    if raw_text.startswith("```"):
        lines = [ln for ln in raw_text.splitlines() if not ln.strip().startswith("```")]
        raw_text = "\n".join(lines).strip()
    return json.loads(raw_text)


def register_file(uploaded_file):
    raw_bytes = uploaded_file.getvalue()
    if not raw_bytes:
        st.session_state["last_status"] = ("error", f"{uploaded_file.name}: 空のファイルです。")
        return

    digest = hashlib.sha256(raw_bytes).hexdigest()
    if digest in st.session_state["processed_hashes"]:
        st.session_state["last_status"] = ("warning", f"「{uploaded_file.name}」は登録済みです。")
        return

    fname_lower = (uploaded_file.name or "").lower()
    ftype_lower = (uploaded_file.type or "").lower()

    if ftype_lower == "application/pdf" or fname_lower.endswith(".pdf"):
        payload_bytes = raw_bytes
        mime_type = "application/pdf"
        ext = "pdf"
    elif fname_lower.endswith(".heic") or "heic" in ftype_lower:
        payload_bytes = optimize_image(raw_bytes)
        mime_type = "image/jpeg" if payload_bytes != raw_bytes else "image/heic"
        ext = "jpg" if mime_type == "image/jpeg" else "heic"
    else:
        payload_bytes = optimize_image(raw_bytes)
        mime_type = "image/jpeg"
        ext = "jpg"

    parsed = analyze_receipt(payload_bytes, mime_type=mime_type)

    seq = len(st.session_state["records"]) + 1
    date_str = str(parsed.get("date") or datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    img_filename = f"{date_str}_{seq:03d}.{ext}" if st.session_state["keep_images"] else ""

    row = {
        "日付": str(parsed.get("date") or ""),
        "取引先・店名": str(parsed.get("store") or ""),
        "勘定科目": str(parsed.get("category") or "雑費"),
        "税込金額": int(parsed.get("amount") or 0),
        "税率(%)": int(parsed.get("tax_rate") or 10),
        "消費税額": int(parsed.get("tax_amount") or 0),
        "インボイス番号": str(parsed.get("invoice_no") or ""),
        "支払方法": str(parsed.get("payment_method") or ""),
        "摘要・品目": str(parsed.get("summary") or ""),
        "画像ファイル名": img_filename,
    }

    st.session_state["records"].append(row)
    st.session_state["processed_hashes"].add(digest)
    if st.session_state["keep_images"]:
        st.session_state["saved_images"][img_filename] = payload_bytes

    st.session_state["export_dirty"] = True
    st.session_state["last_status"] = (
        "success",
        f"登録完了：{row['日付']} / {row['取引先・店名']} / {row['勘定科目']} / ¥{row['税込金額']:,}",
    )


st.markdown("### 撮るだけ確定申告 Pro")
st.caption("レシート入力・仕分けの下書きを最速で終わらせる相棒")

if st.session_state["is_pro"]:
    email_disp = st.session_state.get("pro_email", "")
    status_text = st.session_state.get("pro_plan_label") or "Pro版有効中"
    st.caption(f"{status_text}（{email_disp}）" if email_disp else status_text)
    portal_url = get_secret("STRIPE_PORTAL_URL")
    with st.expander("ご契約管理・解約 / 認証解除"):
        if portal_url:
            st.caption("契約内容の確認・お支払い方法の変更・解約（退会）はこちら")
            st.link_button("契約管理・解約画面を開く", portal_url, use_container_width=True)
        if st.button("認証を解除する", use_container_width=True):
            clear_pro_session(reset_browser=True)
            st.session_state["last_status"] = ("warning", "Pro認証を解除しました。")
            st.rerun()
else:
    used_count = len(st.session_state["records"])
    rem_count = max(0, FREE_TRIAL_LIMIT - used_count)
    st.caption(f"無料お試し 残り {rem_count}/{FREE_TRIAL_LIMIT} 件")
    with st.expander("Pro購入 / メールアドレス・パスコード認証", expanded=(rem_count == 0)):
        st.caption("年額 3,960円（税込／月あたり 330円相当・毎年自動更新）")
        stripe_url = get_secret("STRIPE_URL", "STRIPE_PAYMENT_LINK")
        if stripe_url:
            st.link_button("Pro購入（年額 3,960円・税込）", stripe_url, use_container_width=True)

        st.caption("① 決済時のメールアドレス（またはモニター登録済みアドレス）で有効化：")
        entered_email = st.text_input(
            "メールアドレス",
            placeholder="name@example.com",
            key="pro_email_input",
        )
        if st.button("メールアドレスで確認して有効化", use_container_width=True):
            with st.spinner("契約状況を確認中..."):
                ok, plan_label, msg = verify_email_subscription(entered_email)
            if ok:
                save_pro_session(entered_email, plan_label)
                st.session_state["last_status"] = ("success", msg)
                st.rerun()
            else:
                st.error(msg)

        valid_codes = get_valid_passcodes()
        if valid_codes:
            st.caption("② パスコードで有効化：")
            entered_code = st.text_input("パスコード", type="password", key="unlock_code_input")
            if st.button("パスコードを有効化", use_container_width=True):
                if entered_code.strip() in valid_codes:
                    st.session_state["is_pro"] = True
                    st.session_state["pro_email"] = ""
                    st.session_state["pro_plan_label"] = "Pro版有効中（パスコード認証）"
                    st.session_state["last_status"] = ("success", "Pro版を有効化しました。")
                    st.rerun()
                else:
                    st.error("パスコードが一致しません。")

keep_images_val
