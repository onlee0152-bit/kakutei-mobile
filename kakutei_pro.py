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
        "cam_key": 0,
        "up_key": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def apply_camera_capture_preference():
    components.html(
        """
        <script>
        (function() {
            function tagCameraInputs() {
                const doc = (window.parent && window.parent.document) || document;
                const uploaders = doc.querySelectorAll('[data-testid="stFileUploader"]');
                uploaders.forEach(function(box) {
                    const txt = box.innerText || "";
                    if (txt.includes("写真を撮る")) {
                        const inp = box.querySelector('input[type="file"]');
                        if (inp) {
                            inp.setAttribute("capture", "environment");
                        }
                    }
                });
            }
            tagCameraInputs();
            setTimeout(tagCameraInputs, 300);
            setTimeout(tagCameraInputs, 1000);
        })();
        </script>
        """,
        height=0,
    )


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
            zf.writestr(excel_name
