import hashlib
import io
import json
import os
from datetime import datetime
import pandas as pd
import streamlit as st
from PIL import Image, ImageEnhance, ImageOps
import pillow_heif
from google import genai
from google.genai import types

pillow_heif.register_heif_opener()

DB_PATH = os.path.expanduser("~/expenses_db.csv")
COLUMNS = ["登録日時", "発生日", "取引先", "勘定科目", "金額", "税率", "インボイス番号", "備考"]
ACCOUNTS = ["消耗品費", "旅費交通費", "通信費", "会議費", "接待交際費", "新聞図書費", "荷造運賃", "外注工賃", "地代家賃", "水道光熱費", "雑費"]

st.set_page_config(page_title="撮るだけ確定申告", layout="centered")
st.title("📸 撮るだけ確定申告（自動蓄積版）")

# APIキーを安全に取得
try:
    api_key = st.secrets.get("GEMINI_API_KEY", "")
except Exception:
    api_key = os.environ.get("GEMINI_API_KEY", "")

with st.sidebar:
    st.header("⚙️ 設定")
    if api_key:
        st.success("APIキー自動読み込み済み")
    else:
        api_key = st.text_input("Gemini API Key", type="password")
    model_name = st.text_input("使用モデル", value="gemini-3.1-flash-lite")

def load_db() -> pd.DataFrame:
    if os.path.exists(DB_PATH):
        return pd.read_csv(DB_PATH)
    return pd.DataFrame(columns=COLUMNS)

def save_db(df: pd.DataFrame):
    df.to_csv(DB_PATH, index=False, encoding="utf-8-sig")

def optimize_image(file_bytes: bytes, max_size: int = 1280) -> bytes:
    img = Image.open(io.BytesIO(file_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    img = ImageEnhance.Contrast(img).enhance(1.15)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=75, optimize=True)
    return out.getvalue()

def analyze_receipt(client: genai.Client, model: str, file_bytes: bytes, mime_type: str) -> dict:
    prompt = f"""
    あなたは日本の個人事業主・副業の確定申告の専門家です。
    添付のレシートから以下を抽出し、JSONのみで返してください。
    - 発生日: YYYY-MM-DD形式（不明なら空文字）
    - 取引先: 店名・支払先
    - 勘定科目: {ACCOUNTS} から最適なものを1つ選択
    - 金額: 合計金額（整数のみ、カンマなし）
    - 税率: "10%", "8%（軽減）", "対象外" のいずれか
    - インボイス番号: Tから始まる13桁の番号（なければ空文字）
    - 備考: 品目や用途の要約（20文字以内）
    """
    response = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=file_bytes, mime_type=mime_type), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
    )
    return json.loads(response.text)

def process_and_append(raw_bytes: bytes, is_pdf: bool = False):
    file_hash = hashlib.md5(raw_bytes).hexdigest()
    if "processed_hashes" not in st.session_state:
        st.session_state.processed_hashes = set()
    if file_hash in st.session_state.processed_hashes:
        return  # 同じ画像の二重登録を防止

    if not api_key:
        st.error("APIキーが設定されていません。左のサイドバーまたは secrets.toml を設定してください。")
        return

    with st.spinner("軽量化＆AI解析中..."):
        try:
            send_bytes = raw_bytes if is_pdf else optimize_image(raw_bytes)
            mime_type = "application/pdf" if is_pdf else "image/jpeg"
            client = genai.Client(api_key=api_key)
            res = analyze_receipt(client, model_name, send_bytes, mime_type)

            new_row = {
                "登録日時": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "発生日": res.get("発生日", ""),
                "取引先": res.get("取引先", ""),
                "勘定科目": res.get("勘定科目", "雑費"),
                "金額": int(res.get("金額", 0) or 0),
                "税率": res.get("税率", "10%"),
                "インボイス番号": res.get("インボイス番号", ""),
                "備考": res.get("備考", ""),
            }
            df = load_db()
            df = pd.concat([pd.DataFrame([new_row]), df], ignore_index=True)
            save_db(df)
            st.session_state.processed_hashes.add(file_hash)
            st.success(f"✅ 自動登録完了：{new_row['取引先']} / ¥{new_row['金額']:,}（{new_row['勘定科目']}）")
        except Exception as e:
            st.error(f"解析エラー: {e}")

tab1, tab2 = st.tabs(["📸 撮って即登録", "📊 蓄積データ・Excel出力"])

with tab1:
    st.caption("シャッターを押すだけで自動解析＆保存されます。")
    cam_img = st.camera_input("レシートを撮影")
    if cam_img is not None:
        process_and_append(cam_img.getvalue(), is_pdf=False)

    st.markdown("---")
    up_file = st.file_uploader("写真ライブラリ（HEIC/JPG/PNG）やPDFから1枚ずつ自動登録", type=["heic", "heif", "jpg", "jpeg", "png", "pdf"])
    if up_file is not None:
        process_and_append(up_file.getvalue(), is_pdf=up_file.name.lower().endswith(".pdf"))

with tab2:
    df = load_db()
    if df.empty:
        st.info("まだ登録されたレシートがありません。「📸 撮って即登録」から撮影してください。")
    else:
        st.subheader(f"📝 蓄積データ（全 {len(df)} 件）")
        edited_df = st.data_editor(
            df,
            num_rows="dynamic",
            use_container_width=True,
            column_config={
                "金額": st.column_config.NumberColumn("金額（円）", format="%d"),
                "勘定科目": st.column_config.SelectboxColumn("勘定科目", options=ACCOUNTS),
                "税率": st.column_config.SelectboxColumn("税率", options=["10%", "8%（軽減）", "対象外"]),
            },
        )
        if st.button("💾 修正内容を保存"):
            save_db(edited_df)
            st.success("保存しました。")

        summary_df = edited_df.groupby("勘定科目", as_index=False)["金額"].sum().sort_values("金額", ascending=False)
        total_row = pd.DataFrame([{"勘定科目": "合計", "金額": int(summary_df["金額"].sum())}])
        summary_df = pd.concat([summary_df, total_row], ignore_index=True)

        st.subheader("📊 勘定科目別 集計")
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            edited_df.to_excel(writer, index=False, sheet_name="経費明細")
            summary_df.to_excel(writer, index=False, sheet_name="勘定科目別集計")

        st.download_button(
            label="📥 確定申告用 Excel (.xlsx) をダウンロード",
            data=output.getvalue(),
            file_name="kakutei_shinkoku_all.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
