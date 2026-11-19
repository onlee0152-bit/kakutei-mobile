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

# シンプルで違和感のない微調整CSS（右上メニュー・フッター非表示＋ボタン角丸の自然な調整）
st.markdown(
    """
    <style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    .stAppDeployButton {display: none;}
    [data-testid="stToolbarActions"] {display: none;}
    [data-testid="manage-app-button"] {display: none;}
    [data-testid="stDecoration"] {display: none;}

    .stButton > button, .stDownloadButton > button, .stLinkButton > a {
        border-radius: 8px;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 10px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

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
if "last_messages" not in st.session_state:
    st.session_state.last_messages = []
if "save_light_image" not in st.session_state:
    st.session_state.save_light_image = False
if "latest_light_image" not in st.session_state:
    st.session_state.latest_light_image = None  # {"bytes": ..., "filename": ...}
if "cached_excel" not in st.session_state:
    st.session_state.cached_excel = None  # {"bytes": ..., "filename": ..., "count": ..., "saved_at": ...}

# 専用URL（?code=〇〇）による自動ロック解除
try:
    url_code = st.query_params.get("code", "")
    if isinstance(url_code, list):
        url_code = url_code[0] if url_code else ""
    if url_code and url_code.strip() == PRO_PASSCODE:
        st.session_state.is_pro = True
except Exception:
    pass

# サイドバー：プラン管理・パスコード解除
with st.sidebar:
    st.header("🔑 プラン・ロック解除")
    if st.session_state.is_pro:
        st.success("✅ Pro版（無制限）が有効です")
        st.caption("📱 この画面をスマホの「ホーム画面に追加」しておくと、次回から1タップでPro版が開きます。")
    else:
        used_count = len(st.session_state.expenses_df)
        st.info(f"無料お試し中：{used_count} / {FREE_LIMIT} 枚")
        st.link_button("💳 Pro版を購入して無制限にする", STRIPE_URL, type="primary", use_container_width=True)
        st.markdown("---")
        input_code = st.text_input("購入後のパスコードを入力", type="password", placeholder="パスコード")
        if st.button("ロック解除", use_container_width=True):
            if input_code.strip() == PRO_PASSCODE:
                st.session_state.is_pro = True
                st.query_params["code"] = PRO_PASSCODE
                st.rerun()
            else:
                st.error("パスコードが正しくありません。")

    st.markdown("---")
    st.caption("🔒 プライバシー保護：画像と集計データはサーバーに永続保存されません。ダウンロード操作を行うまで画面上のデータは保持されます。")

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

def build_excel_bytes(detail_df: pd.DataFrame, summary_df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        detail_df.to_excel(writer, index=False, sheet_name="経費明細")
        summary_df.to_excel(writer, index=False, sheet_name="勘定科目別集計")
    return output.getvalue()

def process_and_append(raw_bytes: bytes, is_pdf: bool = False) -> bool:
    file_hash = hashlib.md5(raw_bytes).hexdigest()
    if file_hash in st.session_state.processed_hashes:
        return False

    # 無料枠チェック
    if not st.session_state.is_pro and len(st.session_state.expenses_df) >= FREE_LIMIT:
        st.warning(f"無料お試しの上限（{FREE_LIMIT}枚）に達しました。続けて利用するにはPro版のパスコードを入力してください。")
        st.link_button("💳 Pro版を購入して無制限にする", STRIPE_URL, type="primary")
        return False

    if not api_key:
        st.error("システムエラー：APIキーが設定されていません。管理者にお問い合わせください。")
        return False

    with st.spinner("軽量化＆AI解析中..."):
        try:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            if is_pdf:
                send_bytes = raw_bytes
                mime_type = "application/pdf"
            else:
                send_bytes = optimize_image(raw_bytes)
                mime_type = "image/jpeg"
                if st.session_state.save_light_image:
                    st.session_state.latest_light_image = {
                        "bytes": send_bytes,
                        "filename": f"receipt_light_{stamp}.jpg",
                    }

            client = genai.Client(api_key=api_key)
            res = analyze_receipt(client, MODEL_NAME, send_bytes, mime_type)

            new_row = {
                "登録日時": now_str,
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
            st.session_state.last_messages.append(
                f"✅ 登録完了：{new_row['取引先']} / ¥{new_row['金額']:,}（{new_row['勘定科目']}）"
            )
            return True
        except Exception as e:
            st.error(f"解析エラー: {e}")
            return False

tab1, tab2 = st.tabs(["📸 撮って即登録", "📊 集計・Excel出力"])

with tab1:
    # 直前の登録完了メッセージを表示してクリア
    if st.session_state.last_messages:
        for msg in st.session_state.last_messages:
            st.success(msg)
        st.session_state.last_messages = []

    # 軽量化画像をiPhone等に保存するかどうかのチェックボックス
    st.checkbox(
        "軽量化したレシート画像（JPEG）を保存する",
        key="save_light_image",
        help="オンにすると、解析後に軽量化された画像を保存ボタンからダウンロードできます。",
    )

    if st.session_state.save_light_image and st.session_state.latest_light_image is not None:
        st.download_button(
            label=f"🖼️ 軽量化画像を保存（{st.session_state.latest_light_image['filename']}）",
            data=st.session_state.latest_light_image["bytes"],
            file_name=st.session_state.latest_light_image["filename"],
            mime="image/jpeg",
        )

    remaining = FREE_LIMIT - len(st.session_state.expenses_df)
    if not st.session_state.is_pro:
        if remaining > 0:
            st.info(f"無料お試し：あと **{remaining} 枚** 登録できます（4枚目以降はPro版で無制限）")
        else:
            st.warning("無料お試し枠（3枚）を使い切りました。左上の「>」からPro版へアップグレードしてください。")
            st.link_button("💳 Pro版を購入して無制限にする", STRIPE_URL, type="primary")
    else:
        st.caption("📱 ブラウザの共有メニューから「ホーム画面に追加」しておくと、次回から1タップで開けます。")

    cam_img = st.camera_input("レシートを撮影")
    if cam_img is not None:
        if process_and_append(cam_img.getvalue(), is_pdf=False):
            st.rerun()

    st.markdown("---")
    up_files = st.file_uploader(
        "写真ライブラリ（HEIC/JPG/PNG）やPDFからまとめて登録",
        type=["heic", "heif", "jpg", "jpeg", "png", "pdf"],
        accept_multiple_files=True,
    )
    if up_files:
        added_any = False
        for f in up_files:
            if process_and_append(f.getvalue(), is_pdf=f.name.lower().endswith(".pdf")):
                added_any = True
        if added_any:
            st.rerun()

with tab2:
    df = st.session_state.expenses_df

    # ダウンロード失敗・やり直し用の「一時保管中Excel」リカバリ枠
    if st.session_state.cached_excel is not None:
        cached = st.session_state.cached_excel
        with st.container(border=True):
            st.caption(
                f"💾 一時保管中のExcelあり（作成：{cached['saved_at']} / {cached['count']}件）。"
                "ダウンロードに失敗した場合や再取得したい場合はこちらからどうぞ。"
            )
            st.download_button(
                label=f"📥 一時保管中のExcelを再ダウンロード（{cached['filename']}）",
                data=cached["bytes"],
                file_name=cached["filename"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="redownload_cached_excel",
            )

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

        excel_bytes = build_excel_bytes(edited_df, summary_df)
        excel_filename = f"kakutei_shinkoku_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"

        # セッション内に一時保管（ダウンロード後も明細は自動消去しない）
        st.session_state.cached_excel = {
            "bytes": excel_bytes,
            "filename": excel_filename,
            "count": len(edited_df),
            "saved_at": datetime.now().strftime("%H:%M:%S"),
        }

        st.download_button(
            label="📥 確定申告用 Excel (.xlsx) をダウンロード",
            data=excel_bytes,
            file_name=excel_filename,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
            key="main_download_excel",
        )

        st.markdown("---")
        if st.button("🗑️ 現在のデータをすべてクリア"):
            st.session_state.expenses_df = pd.DataFrame(columns=COLUMNS)
            st.session_state.processed_hashes = set()
            st.session_state.latest_light_image = None
            st.rerun()

st.markdown("---")
st.caption(
    "※免責事項：本ツールはAIによる自動仕分け支援ツールです。最終的な税務申告の内容・勘定科目の妥当性については、必ずご自身または税理士にご確認ください。"
    "アップロードされた画像およびデータは解析後に即時破棄され、サーバーには一切保存されません。"
)
