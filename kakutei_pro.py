import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import io
import zipfile
import json
import hashlib
from datetime import datetime
from PIL import Image, ImageOps
from google import genai
from google.genai import types

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
    [data-testid="stCameraInput"] > div > button {
        font-size: 0 !important;
        line-height: 0 !important;
    }
    [data-testid="stCameraInput"] > div > button::after {
        content: "⭕️";
        font-size: 40px !important;
        line-height: 1 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

PRIMARY_MODEL = "gemini-3.1-flash-lite"
FREE_TRIAL_LIMIT = 3
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


def pro_token(code: str) -> str:
    return hashlib.sha256(f"kakutei_pro_v1:{code}".encode("utf-8")).hexdigest()


def init_session():
    defaults = {
        "is_pro": False,
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
        "last_cam_digest": None,
        "camera_facing": "environment",
        "camera_patch_applied": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def save_pro_to_browser(code: str):
    if not code:
        return
    token = pro_token(code)
    try:
        st.query_params["pro"] = token
    except Exception:
        pass
    components.html(
        f"""
        <script>
        (function() {{
            const token = "{token}";
            try {{
                localStorage.setItem("kakutei_pro_token", token);
                if (window.parent && window.parent.localStorage) {{
                    window.parent.localStorage.setItem("kakutei_pro_token", token);
                }}
            }} catch (e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


def restore_pro_from_browser(valid_code: str):
    if st.session_state.get("is_pro") or not valid_code:
        return
    expected_token = pro_token(valid_code)
    components.html(
        f"""
        <script>
        (function() {{
            const expected = "{expected_token}";
            try {{
                const saved = localStorage.getItem("kakutei_pro_token") ||
                    (window.parent && window.parent.localStorage ? window.parent.localStorage.getItem("kakutei_pro_token") : "");
                if (saved === expected) {{
                    const parentWin = window.parent || window;
                    const url = new URL(parentWin.location.href);
                    if (url.searchParams.get("pro") !== expected) {{
                        url.searchParams.set("pro", expected);
                        parentWin.history.replaceState(null, "", url.toString());
                        parentWin.location.reload();
                    }}
                }}
            }} catch (e) {{}}
        }})();
        </script>
        """,
        height=0,
    )


def sync_pro_status():
    valid_code = get_secret("PRO_PASSCODE", "ACCESS_CODE")
    if not valid_code:
        st.session_state["is_pro"] = True
        return

    expected_token = pro_token(valid_code)
    query_pro = str(st.query_params.get("pro", "")).strip()
    query_code = str(st.query_params.get("code", "")).strip()

    if query_pro == expected_token or (query_code and query_code == valid_code):
        st.session_state["is_pro"] = True
        save_pro_to_browser(valid_code)
    elif not st.session_state.get("is_pro"):
        restore_pro_from_browser(valid_code)


init_session()
sync_pro_status()


def apply_camera_facing_preference(facing: str):
    if st.session_state.get("camera_patch_applied"):
        return
    st.session_state["camera_patch_applied"] = True
    components.html(
        f"""
        <script>
        (function() {{
            const preferredFacing = "{facing}";
            const targets = [navigator.mediaDevices];
            if (window.parent && window.parent.navigator && window.parent.navigator.mediaDevices) {{
                targets.push(window.parent.navigator.mediaDevices);
            }}
            targets.forEach(function(mediaDevices) {{
                if (!mediaDevices || !mediaDevices.getUserMedia || mediaDevices._kakuteiPatched) return;
                mediaDevices._kakuteiPatched = true;
                const origGetUserMedia = mediaDevices.getUserMedia.bind(mediaDevices);
                mediaDevices.getUserMedia = function(constraints) {{
                    let nextConstraints = constraints;
                    if (constraints && constraints.video) {{
                        const v = typeof constraints.video === "object" ? {{ ...constraints.video }} : {{}};
                        if (!v.facingMode && !v.deviceId) {{
                            v.facingMode = {{ ideal: preferredFacing }};
                        }}
                        nextConstraints = {{ ...constraints, video: v }};
                    }}
                    return origGetUserMedia(nextConstraints).catch(function(err) {{
                        if (nextConstraints && nextConstraints.video && nextConstraints.video.facingMode) {{
                            const fb = typeof nextConstraints.video === "object" ? {{ ...nextConstraints.video }} : {{}};
                            delete fb.facingMode;
                            return origGetUserMedia({{ ...nextConstraints, video: fb }});
                        }}
                        throw err;
                    }});
                }};
            }});
        }})();
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
        # PILで開けない形式（未対応HEIC等）は元のバイト列をそのまま返す
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

if st.session_state["is_pro"]:
    st.caption("Pro版有効中")
else:
    used_count = len(st.session_state["records"])
    rem_count = max(0, FREE_TRIAL_LIMIT - used_count)
    st.caption(f"無料お試し 残り {rem_count}/{FREE_TRIAL_LIMIT} 件")
    with st.expander("Pro解除 / 申し込み", expanded=(rem_count == 0)):
        stripe_url = get_secret("STRIPE_URL", "STRIPE_PAYMENT_LINK")
        if stripe_url:
            st.link_button("年額 4,980円（税込）を申し込む", stripe_url, use_container_width=True)
        entered_code = st.text_input("パスコード", type="password", key="unlock_code_input")
        if st.button("解除", use_container_width=True):
            valid_code = get_secret("PRO_PASSCODE", "ACCESS_CODE")
            if valid_code and entered_code.strip() == valid_code:
                st.session_state["is_pro"] = True
                save_pro_to_browser(valid_code)
                st.session_state["last_status"] = ("success", "Pro版を解除しました。")
                st.rerun()
            else:
                st.error("パスコードが一致しません。")

keep_images_val = st.toggle("画像もZIPに同梱する", value=st.session_state["keep_images"])
if keep_images_val != st.session_state["keep_images"]:
    st.session_state["keep_images"] = keep_images_val
    if not keep_images_val:
        st.session_state["saved_images"] = {}
    st.session_state["export_dirty"] = True

total_records = len(st.session_state["records"])
warn_threshold = WARN_LIMIT_WITH_IMAGES if st.session_state["keep_images"] else WARN_LIMIT_TEXT_ONLY
if total_records >= warn_threshold:
    st.warning(f"現在 {total_records} 件登録されています。保存してください。")

tab_input, tab_export = st.tabs([f"登録 ({total_records})", "出力"])

with tab_input:
    allowed = st.session_state["is_pro"] or (total_records < FREE_TRIAL_LIMIT)
    if not allowed:
        st.warning("無料枠（3件）に達しました。")
    else:
        apply_camera_facing_preference(st.session_state["camera_facing"])
        cam_photo = st.camera_input("撮影", key="cam_fixed")
        if cam_photo is not None:
            cam_digest = hashlib.sha256(cam_photo.getvalue()).hexdigest()
            if cam_digest != st.session_state["last_cam_digest"]:
                st.session_state["last_cam_digest"] = cam_digest
                with st.spinner("解析中..."):
                    try:
                        register_file(cam_photo)
                    except Exception as exc:
                        st.session_state["last_status"] = ("error", f"エラー: {exc}")

        picked_files = st.file_uploader(
            "画像・PDFを選択",
            type=["jpg", "jpeg", "png", "webp", "heic", "pdf"],
            accept_multiple_files=True,
            key="up_fixed",
        )
        if picked_files:
            for f in picked_files:
                raw_check = f.getvalue()
                if not raw_check:
                    continue
                f_digest = hashlib.sha256(raw_check).hexdigest()
                if f_digest in st.session_state["processed_hashes"]:
                    continue
                if not st.session_state["is_pro"] and len(st.session_state["records"]) >= FREE_TRIAL_LIMIT:
                    st.session_state["last_status"] = ("warning", "無料枠（3件）に達しました。")
                    break
                with st.spinner(f"解析中: {f.name}"):
                    try:
                        register_file(f)
                    except Exception as exc:
                        st.session_state["last_status"] = ("error", f"{f.name}: {exc}")

    if st.session_state["last_status"]:
        level, text = st.session_state["last_status"]
        if level == "success":
            st.success(text)
        elif level == "warning":
            st.warning(text)
        else:
            st.error(text)

with tab_export:
    if not st.session_state["records"]:
        st.info("データがありません。")
    else:
        if st.session_state["export_dirty"] or (
            st.session_state["excel_bytes"] is None
            and st.session_state["zip_bytes"] is None
        ):
            rebuild_export_files()

        df_view = pd.DataFrame(st.session_state["records"])
        sum_amount = int(pd.to_numeric(df_view["税込金額"], errors="coerce").fillna(0).sum())
        col1, col2 = st.columns(2)
        col1.metric("件数", f"{len(df_view)}")
        col2.metric("合計", f"¥{sum_amount:,}")

        edited = st.data_editor(
            df_view,
            num_rows="dynamic",
            use_container_width=True,
            key="detail_editor",
        )
        if not edited.equals(df_view):
            st.session_state["records"] = edited.to_dict(orient="records")
            rebuild_export_files()

        if st.session_state["keep_images"] and st.session_state["zip_bytes"]:
            st.download_button(
                label="ZIP一括ダウンロード",
                data=st.session_state["zip_bytes"],
                file_name=st.session_state["zip_name"],
                mime="application/zip",
                use_container_width=True,
            )
        elif st.session_state["excel_bytes"]:
            st.download_button(
                label="Excelダウンロード (.xlsx)",
                data=st.session_state["excel_bytes"],
                file_name=st.session_state["excel_name"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        if st.button("データクリア", use_container_width=True):
            st.session_state["records"] = []
            st.session_state["saved_images"] = {}
            st.session_state["processed_hashes"] = set()
            st.session_state["zip_bytes"] = None
            st.session_state["zip_name"] = ""
            st.session_state["excel_bytes"] = None
            st.session_state["excel_name"] = ""
            st.session_state["export_dirty"] = False
            st.session_state["last_cam_digest"] = None
            st.session_state["last_status"] = ("warning", "クリアしました。")
            st.rerun()
            
