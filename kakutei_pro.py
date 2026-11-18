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

COLUMNS = ["登録日時", "発生日", "取引先", "勘定科目", "金額", "税率", "インボイス番号", "備考"]
ACCOUNTS = [
    "消耗品費", "旅費交通費", "通信費", "会議費", "接待交際費",
    "新聞図書費", "荷造運賃", "外注工賃", "地代家賃", "水道光熱費", "雑費"
]
FREE_LIMIT = 3

st.set_page_config(page_title="撮るだけ確定申告 Pro", layout="centered")

# Secrets / 環境変数の安全な読み込み
def get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, os.environ.get(key, default))
    except Exception:
        return os.environ.get(key, default)

api_key = get_secret("GEMINI_API_KEY", "")
PRO_PASSCODE = get_secret("PRO_PASSCODE", "KAKUTEI2026")
STRIPE_URL = get_secret("STRIPE_URL", "https://buy.stripe.com/test_your_link_here")
MODEL_NAME = get_secret("GEMINI_MODEL", "gemini-3.1-flash-lite")

# セッション状態の初期化（ユーザーごとに完全分離・サーバー非保存）
if "expenses_df" not in st.session_state:
    st.session_state.expenses_df = pd.DataFrame(columns=COLUMNS)
if "processed_hashes" not in st.session_state:
    st.session_state.processed_hashes = set()
if "is_pro" not in st.session_state:
    st.session_state.is_pro = False

# サイドバー：プラン管理・パスコード解除
with st.sidebar:
    st.header("🔑 プラン・ロック解除")
    if st.session_state.is_pro:
        st.success("✅ Pro版（無制限）が有効です")
    else:
        used_count = len(st.session_state.expenses_df)
        st.info(f"無料お試し中：{used_count} / {FREE_LIMIT} 枚")
        st.link_button("💳 Pro版を購入して無制限にする", STRIPE_URL, type="primary", use_container_width=True)
        st.markdown("---")
        input_code = st.text_input("購入後のパスコードを入力", type="password", placeholder="パスコード")
        if st.button("ロック解除", use_container_width=True):
            if input_code.strip() == PRO_PASSCODE:
                st.session_state.is_pro = True
                st.success("Pro版が有効になりました！")
                st.rerun()
            else:
                st.error("パスコードが正しくありません。")

    st.markdown("---")
    st.caption("🔒 プライバシー保護：アップロードされた画像と集計データはサーバーに一切保存されず、画面を閉じると完全に消去されます。")

st.title("📸 撮るだけ確定申告 Pro")
st.caption("レシートを撮るだけでAIが勘定科目・税率・インボイス番号を自動仕分けし、確定申告用Excelを作成します。")

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
    if file_hash in st.session_state.processed_hashes:
        return

    # 無料枠チェック
    if not st.session_state.is_pro and len(st.session_state.expenses_df) >= FREE_LIMIT:
        st.warning(f"無料お試しの上限（{FREE_LIMIT}枚）に達しました。続けて利用するにはPro版のパスコードを入力してください。")
        st.link_button("💳 Pro版を購入して無制限にする", STRIPE_URL, type="primary")
        return

    if not api_key:
        st.error("システムエラー：APIキーが設定されていません。管理者にお問い合わせください。")
        return

    with st.spinner("軽量化＆AI解析中..."):
        try:
            send_bytes = raw_bytes if is_pdf else optimize_image(raw_bytes)
            mime_type = "application/pdf" if is_pdf else "image/jpeg"
            client = genai.Client(api_key=api_key)
            res = analyze_receipt(client, MODEL_NAME, send_bytes, mime_type)

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
            st.session_state.expenses_df = pd.concat(
                [pd.DataFrame([new_row]), st.session_state.expenses_df],
                ignore_index=True
            )
            st.session_state.processed_hashes.add(file_hash)
            st.success(f"✅ 登録完了：{new_row['取引先']} / ¥{new_row['金額']:,}（{new_row['勘定科目']}）")
        except Exception as e:
            st.error(f"解析エラー: {e}")

tab1, tab2 = st.tabs(["📸 撮って即登録", "📊 集計・Excel出力"])

with tab1:
    remaining = FREE_LIMIT - len(st.session_state.expenses_df)
    if not st.session_state.is_pro:
        if remaining > 0:
            st.info(f"無料お試し：あと **{remaining} 枚** 登録できます（4枚目以降はPro版で無制限）")
        else:
            st.warning("無料お試し枠（3枚）を使い切りました。左のメニュー（スマホは左上の「>」）からPro版へアップグレードしてください。")
            st.link_button("💳 Pro版を購入して無制限にする", STRIPE_URL, type="primary")

    cam_img = st.camera_input("レシートを撮影")
    if cam_img is not None:
        process_and_append(cam_img.getvalue(), is_pdf=False)

    st.markdown("---")
    up_files = st.file_uploader(
        "写真ライブラリ（HEIC/JPG/PNG）やPDFからまとめて登録",
        type=["heic", "heif", "jpg", "jpeg", "png", "pdf"],
        accept_multiple_files=True,
    )
    if up_files:
        for f in up_files:
            process_and_append(f.getvalue(), is_pdf=f.name.lower().endswith(".pdf"))

with tab2:
    df = st.session_state.expenses_df
    if df.empty:
        st.info("まだ登録されたレシートがありません。「📸 撮って即登録」から撮影またはアップロードしてください。")
    else:
        st.subheader(f"📝 経費明細の確認・修正（全 {len(df)} 件）")
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
        st.session_state.expenses_df = edited_df

        summary_df = (
            edited_df.groupby("勘定科目", as_index=False)["金額"]
            .sum()
            .sort_values("金額", ascending=False)
        )
        total_row = pd.DataFrame([{"勘定科目": "合計", "金額": int(summary_df["金額"].sum())}])
        summary_df = pd.concat([summary_df, total_row], ignore_index=True)

        st.subheader("📊 勘定科目別 集計（確定申告・収支内訳書用）")
        st.dataframe(summary_df, use_container_width=True, hide_index=True)

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            edited_df.to_excel(writer, index=False, sheet_name="経費明細")
            summary_df.to_excel(writer, index=False, sheet_name="勘定科目別集計")

        st.download_button(
            label="📥 確定申告用 Excel (.xlsx) をダウンロード",
            data=output.getvalue(),
            file_name="kakutei_shinkoku_pro.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

        if st.button("🗑️ 現在のデータをすべてクリア"):
            st.session_state.expenses_df = pd.DataFrame(columns=COLUMNS)
            st.session_state.processed_hashes = set()
            st.rerun()

st.markdown("---")
st.caption(
    "※免責事項：本ツールはAIによる自動仕分け支援ツールです。最終的な税務申告の内容・勘定科目の妥当性については、必ずご自身または税理士にご確認ください。"
    "アップロードされた画像およびデータは解析後に即時破棄され、サーバーには一切保存されません。"
)
