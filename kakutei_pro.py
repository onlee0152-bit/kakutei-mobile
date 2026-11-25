import streamlit as st
import pandas as pd
import io
import zipfile
import json
import time
import hashlib
from datetime import datetime
from PIL import Image, ImageOps
from google import genai
from google.genai import types

# ==========================================
# 1. ページ設定・基本定数
# ==========================================
st.set_page_config(
    page_title="撮るだけ確定申告 Pro",
    page_icon="🧾",
    layout="centered",
    initial_sidebar_state="collapsed"
)

PRIMARY_MODEL = st.secrets.get("PRIMARY_MODEL", "gemini-3.1-flash-lite")
FREE_TRIAL_LIMIT = 3
WARN_LIMIT_TEXT_ONLY = 300
WARN_LIMIT_WITH_IMAGES = 150

ACCOUNT_CATEGORIES = [
    "消耗品費", "旅費交通費", "接待交際費", "会議費", "通信費",
    "水道光熱費", "地代家賃", "新聞図書費", "広告宣伝費", "車両費",
    "支払手数料", "外注工賃", "修繕費", "租税公課", "雑費"
]

# ==========================================
# 2. セッション初期化
# ==========================================
def init_session():
    defaults = {
        "is_pro": False,
        "records": [],
        "saved_images": {},
        "keep_images": False,
        "processed_hashes": set(),
        "last_status": None,
        "cached_zip_bytes": None,
        "cached_zip_name": "",
        "cached_excel_bytes": None,
        "cached_excel_name": "",
        "uploader_key": 0,
        "camera_key": 0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_session()

# ==========================================
# 3. Pro認証判定（無料3件お試し＋パスコード解除）
# ==========================================
def sync_pro_status():
    valid_code = str(st.secrets.get("ACCESS_CODE", "")).strip()
    if not valid_code:
        st.session_state["is_pro"] = True
        return

    query_code = str(st.query_params.get("code", "")).strip()
    if query_code and query_code == valid_code:
        st.session_state["is_pro"] = True

sync_pro_status()

# ==========================================
# 4. 画像軽量化・Excel / 一括ZIP事前生成
# ==========================================
def optimize_image(file_bytes: bytes, max_px: int = 1200, quality: int = 72) -> bytes:
    img = Image.open(io.BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_px:
        ratio = max_px / float(max(w, h))
        img = img.resize((int(w * ratio), int(h * ratio)), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()

def build_excel_bytes(records: list) -> bytes:
    df = pd.DataFrame(records)
    cols = [
        "日付", "取引先・店名", "勘定科目", "税込金額", "税率(%)",
        "消費税額", "インボイス番号", "支払方法", "摘要・品目", "画像ファイル名"
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    df = df[cols]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="経費明細一覧")
        summary = (
            df.groupby("勘定科目", as_index=False)["税込金額"]
            .sum()
            .sort_values(by="税込金額", ascending=False)
        )
        summary.to_excel(writer, index=False, sheet_name="勘定科目別集計")
    return output.getvalue()

def refresh_export_cache():
    records = st.session_state["records"]
    if not records:
        st.session_state["cached_excel_bytes"] = None
        st.session_state["cached_zip_bytes"] = None
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    excel_name = f"kakutei_shinkoku_{ts}.xlsx"
    excel_bytes = build_excel_bytes(records)
    st.session_state["cached_excel_bytes"] = excel_bytes
    st.session_state["cached_excel_name"] = excel_name

    if st.session_state["keep_images"] and st.session_state["saved_images"]:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(excel_name, excel_bytes)
            for fname, img_bytes in st.session_state["saved_images"].items():
                zf.writestr(f"receipts/{fname}", img_bytes)
        st.session_state["cached_zip_bytes"] = zip_buf.getvalue()
        st.session_state["cached_zip_name"] = f"kakutei_set_{ts}.zip"
    else:
        st.session_state["cached_zip_bytes"] = None

# ==========================================
# 5. Gemini API 解析処理（高速単一モデル）
# ==========================================
def analyze_receipt(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    api_key = str(st.secrets.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY が設定されていません。")

    client = genai.Client(api_key=api_key)
    prompt = f"""
あなたは日本の確定申告・経理実務に精通したAIアシスタントです。
画像（レシート・領収書・請求書）から以下の項目を抽出し、JSONのみで返してください。

- date: 日付（YYYY-MM-DD形式。不明時は今日の日付）
- store: 取引先・店舗名
- category: 勘定科目（次の中から最も適切な1つを選択: {", ".join(ACCOUNT_CATEGORIES)}）
- amount: 税込合計金額（整数。カンマなし）
- tax_rate: 主な消費税率（10 または 8 または 0）
- tax_amount: 消費税額（整数。記載なければ税込金額から逆算）
- invoice_no: インボイス登録番号（Tから始まる13桁。なければ空文字 ""）
- payment_method: 支払方法（現金 / クレジットカード / 電子マネー・QR / 振込 / 不明）
- summary: 購入した主な品目・用途の簡潔な説明
"""

    response_schema = {
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
        "required": ["date", "store", "category", "amount", "tax_rate", "tax_amount", "invoice_no", "payment_method", "summary"],
    }

    res = client.models.generate_content(
        model=PRIMARY_MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            prompt,
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_schema,
            temperature=0.1,
        ),
    )
    return json.loads(res.text)

def process_uploaded_file(uploaded_file):
    raw_bytes = uploaded_file.getvalue()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    if file_hash in st.session_state["processed_hashes"]:
        st.session_state["last_status"] = ("warning", f"「{uploaded_file.name}」はすでに登録済みです。")
        return

    is_pdf = uploaded_file.type == "application/pdf" or uploaded_file.name.lower().endswith(".pdf")
    if is_pdf:
        send_bytes = raw_bytes
        mime_type = "application/pdf"
        ext = "pdf"
    else:
        send_bytes = optimize_image(raw_bytes)
        mime_type = "image/jpeg"
        ext = "jpg"

    parsed = analyze_receipt(send_bytes, mime_type=mime_type)

    idx = len(st.session_state["records"]) + 1
    safe_date = parsed.get("date", datetime.now().strftime("%Y-%m-%d")).replace("-", "")
    saved_filename = f"{safe_date}_{idx:03d}.{ext}" if st.session_state["keep_images"] else ""

    record = {
        "日付": parsed.get("date", ""),
        "取引先・店名": parsed.get("store", ""),
        "勘定科目": parsed.get("category", "雑費"),
        "税込金額": int(parsed.get("amount", 0)),
        "税率(%)": int(parsed.get("tax_rate", 10)),
        "消費税額": int(parsed.get("tax_amount", 0)),
        "インボイス番号": parsed.get("invoice_no", ""),
        "支払方法": parsed.get("payment_method", ""),
        "摘要・品目": parsed.get("summary", ""),
        "画像ファイル名": saved_filename,
    }

    st.session_state["records"].append(record)
    st.session_state["processed_hashes"].add(file_hash)

    if st.session_state["keep_images"]:
        st.session_state["saved_images"][saved_filename] = send_bytes

    refresh_export_cache()
    st.session_state["last_status"] = (
        "success",
        f"登録完了：{record['日付']} / {record['取引先・店名']} / {record['勘定科目']} / ¥{record['税込金額']:,}"
    )

# ==========================================
# 6. UI構成
# ==========================================
st.title("🧾 撮るだけ確定申告 Pro")

if st.session_state["is_pro"]:
    st.caption("✅ Pro版（年間1,200枚目安）有効中 ｜ 作業の区切りで「一括ダウンロード」を実行して保存してください。")
else:
    remaining = max(0, FREE_TRIAL_LIMIT - len(st.session_state["records"]))
    st.info(f"🎁 無料お試し中（残り {remaining} / {FREE_TRIAL_LIMIT} 件）")
    with st.expander("🔑 Pro版パスコード解除・お申し込みはこちら", expanded=(remaining == 0)):
        stripe_url = st.secrets.get("STRIPE_PAYMENT_LINK", "")
        if stripe_url:
            st.link_button("💳 Pro版（年額 4,980円・税込）を申し込む", stripe_url, use_container_width=True, type="primary")
        code_input = st.text_input("解除パスコードを入力", type="password", key="pro_code_input")
        if st.button("Pro版を解除する", use_container_width=True):
            valid_code = str(st.secrets.get("ACCESS_CODE", "")).strip()
            if valid_code and code_input.strip() == valid_code:
                st.session_state["is_pro"] = True
                st.session_state["last_status"] = ("success", "Pro版のロックを解除しました。続けてレシートを登録できます。")
                st.rerun()
            else:
                st.error("パスコードが正しくありません。")

# 画像保管スイッチ
keep_toggle = st.toggle(
    "レシート画像もZIPに同梱して保管する（オフ時はExcelのみ・高速軽量）",
    value=st.session_state["keep_images"],
)
if keep_toggle != st.session_state["keep_images"]:
    st.session_state["keep_images"] = keep_toggle
    if not keep_toggle:
        st.session_state["saved_images"] = {}
    refresh_export_cache()

count = len(st.session_state["records"])
limit = WARN_LIMIT_WITH_IMAGES if st.session_state["keep_images"] else WARN_LIMIT_TEXT_ONLY
if count >= limit:
    st.warning(f"現在 {count} 件登録されています。安定動作のため、一度「集計・一括ダウンロード」から保存してください。")

tab1, tab2 = st.tabs([f"📸 撮って即登録 ({count}件)", "📊 集計・一括ダウンロード"])

with tab1:
    can_register = st.session_state["is_pro"] or (count < FREE_TRIAL_LIMIT)
    if not can_register:
        st.warning("無料お試しの3件に達しました。上の「Pro版パスコード解除・お申し込み」から解除してください。")
    else:
        cam_file = st.camera_input("レシートを撮影", key=f"cam_{st.session_state['camera_key']}")
        if cam_file is not None:
            with st.spinner("AIがレシートを解析中..."):
                try:
                    process_uploaded_file(cam_file)
                except Exception as e:
                    st.session_state["last_status"] = ("error", f"読み取りエラー: {e}")
            st.session_state["camera_key"] += 1
            st.rerun()

        up_files = st.file_uploader(
            "または画像・PDFを選択（複数可）",
            type=["jpg", "jpeg", "png", "webp", "heic", "pdf"],
            accept_multiple_files=True,
            key=f"up_{st.session_state['uploader_key']}",
        )
        if up_files:
            for f in up_files:
                if not st.session_state["is_pro"] and len(st.session_state["records"]) >= FREE_TRIAL_LIMIT:
                    st.session_state["last_status"] = ("warning", "無料お試しの3件に達したため残りをスキップしました。")
                    break
                with st.spinner(f"解析中: {f.name}"):
                    try:
                        process_uploaded_file(f)
                    except Exception as e:
                        st.session_state["last_status"] = ("error", f"{f.name} の読み取りエラー: {e}")
            st.session_state["uploader_key"] += 1
            st.rerun()

    if st.session_state["last_status"]:
        level, msg = st.session_state["last_status"]
        if level == "success":
            st.success(msg)
        elif level == "warning":
            st.warning(msg)
        else:
            st.error(msg)

with tab2:
    if not st.session_state["records"]:
        st.info("まだ登録データがありません。「撮って即登録」タブからレシートを登録してください。")
    else:
        df_current = pd.DataFrame(st.session_state["records"])
        total_amt = int(df_current["税込金額"].sum())
        c1, c2 = st.columns(2)
        c1.metric("登録件数", f"{len(df_current)} 件")
        c2.metric("税込合計金額", f"¥{total_amt:,}")

        st.subheader("登録明細（直接編集可能）")
        edited_df = st.data_editor(
            df_current,
            num_rows="dynamic",
            use_container_width=True,
            key="records_editor",
        )
        if not edited_df.equals(df_current):
            st.session_state["records"] = edited_df.to_dict(orient="records")
            refresh_export_cache()

        st.divider()
        st.subheader("1回完結ダウンロード（iPhone / PC対応）")
        if st.session_state["keep_images"] and st.session_state["cached_zip_bytes"]:
            st.caption("Excel（.xlsx）と軽量化レシート画像を1つのZIPにまとめて保存します（無圧縮高速ZIPでiPhoneでも即保存できます）。")
            st.download_button(
                label="📦 Excel＋画像を1つのZIPで一括ダウンロード",
                data=st.session_state["cached_zip_bytes"],
                file_name=st.session_state["cached_zip_name"],
                mime="application/zip",
                type="primary",
                use_container_width=True,
            )
        elif st.session_state["cached_excel_bytes"]:
            st.caption("Excel（.xlsx）をダウンロードします。")
            st.download_button(
                label="📥 確定申告用Excelをダウンロード (.xlsx)",
                data=st.session_state["cached_excel_bytes"],
                file_name=st.session_state["cached_excel_name"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                use_container_width=True,
            )

        st.divider()
        if st.button("🗑️ 保存完了後にデータをすべてクリア", use_container_width=True):
            st.session_state["records"] = []
            st.session_state["saved_images"] = {}
            st.session_state["processed_hashes"] = set()
            st.session_state["cached_excel_bytes"] = None
            st.session_state["cached_zip_bytes"] = None
            st.session_state["last_status"] = ("warning", "すべてのデータをクリアしました。")
            st.rerun()
        
