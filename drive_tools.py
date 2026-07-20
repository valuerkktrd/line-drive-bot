"""Google Drive tools สำหรับให้ Gemini เรียกใช้ (function calling)"""

import io
import json
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
ROOT_FOLDER_ID = os.environ["DRIVE_ROOT_FOLDER_ID"]
CLIENT_SECRET_FILE = os.environ.get("GOOGLE_CLIENT_SECRET_FILE", "client_secret.json")
TOKEN_FILE = os.environ.get("GOOGLE_TOKEN_FILE", "token.json")

# บนคลาวด์: ส่ง token ผ่าน env var GOOGLE_TOKEN_JSON แทนไฟล์ (ไฟล์ไม่ถูก commit)
if not os.path.exists(TOKEN_FILE) and os.environ.get("GOOGLE_TOKEN_JSON"):
    with open(TOKEN_FILE, "w", encoding="utf-8") as _f:
        _f.write(os.environ["GOOGLE_TOKEN_JSON"])

_service = None
_sheets_service = None


def _creds():
    """OAuth ในนามบัญชีผู้ใช้ (service account อัปโหลดลง My Drive ไม่ได้แล้ว)"""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    return creds


def drive():
    global _service
    if _service is None:
        _service = build("drive", "v3", credentials=_creds())
    return _service


def sheets():
    global _sheets_service
    if _sheets_service is None:
        _sheets_service = build("sheets", "v4", credentials=_creds())
    return _sheets_service


def _export_big_sheet_csv(file_id: str, path: str) -> None:
    """Google Sheets ใหญ่เกินลิมิต export 10MB — ดึงผ่าน Sheets API ทีละช่วงแล้วเขียนเป็น CSV"""
    import csv

    meta = sheets().spreadsheets().get(
        spreadsheetId=file_id,
        fields="sheets(properties(title,gridProperties(rowCount)))",
    ).execute()
    props = meta["sheets"][0]["properties"]
    title = props["title"]
    total_rows = props["gridProperties"]["rowCount"]

    chunk = 20000
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        r = 1
        while r <= total_rows:
            rng = f"'{title}'!A{r}:AZ{min(r + chunk - 1, total_rows)}"
            vals = sheets().spreadsheets().values().get(
                spreadsheetId=file_id, range=rng,
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute().get("values", [])
            if not vals:
                break
            w.writerows(vals)
            r += chunk


FILE_FIELDS = "id, name, mimeType, modifiedTime, size, parents, webViewLink"


def _fmt(files: list[dict]) -> str:
    if not files:
        return "ไม่พบไฟล์"
    return json.dumps(files, ensure_ascii=False, indent=1)


def search_files(query: str) -> str:
    """ค้นหาไฟล์/โฟลเดอร์ใน Google Drive จากชื่อ (ค้นแบบ contains)

    Args:
        query: คำค้นหาในชื่อไฟล์ เช่น "ราคาประเมิน" หรือ "น้ำพอง"
    """
    q = query.replace("'", "\\'")
    res = drive().files().list(
        q=f"name contains '{q}' and trashed=false",
        fields=f"files({FILE_FIELDS})",
        pageSize=20,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return _fmt(res.get("files", []))


def list_folder(folder_id: str = "") -> str:
    """แสดงรายการไฟล์ในโฟลเดอร์ (ถ้าไม่ระบุ folder_id จะใช้โฟลเดอร์หลักของบอท)

    Args:
        folder_id: ID ของโฟลเดอร์ที่ต้องการดู (เว้นว่าง = โฟลเดอร์หลัก)
    """
    fid = folder_id or ROOT_FOLDER_ID
    res = drive().files().list(
        q=f"'{fid}' in parents and trashed=false",
        fields=f"files({FILE_FIELDS})",
        pageSize=50,
        orderBy="folder,name",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    return _fmt(res.get("files", []))


def create_folder(name: str, parent_id: str = "") -> str:
    """สร้างโฟลเดอร์ใหม่

    Args:
        name: ชื่อโฟลเดอร์
        parent_id: ID โฟลเดอร์แม่ (เว้นว่าง = สร้างในโฟลเดอร์หลักของบอท)
    """
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id or ROOT_FOLDER_ID],
    }
    f = drive().files().create(
        body=meta, fields=FILE_FIELDS, supportsAllDrives=True
    ).execute()
    return _fmt([f])


def move_file(file_id: str, new_parent_id: str) -> str:
    """ย้ายไฟล์/โฟลเดอร์ไปยังโฟลเดอร์ปลายทาง

    Args:
        file_id: ID ของไฟล์ที่จะย้าย
        new_parent_id: ID ของโฟลเดอร์ปลายทาง
    """
    f = drive().files().get(
        fileId=file_id, fields="parents", supportsAllDrives=True
    ).execute()
    prev = ",".join(f.get("parents", []))
    f = drive().files().update(
        fileId=file_id,
        addParents=new_parent_id,
        removeParents=prev,
        fields=FILE_FIELDS,
        supportsAllDrives=True,
    ).execute()
    return _fmt([f])


def rename_file(file_id: str, new_name: str) -> str:
    """เปลี่ยนชื่อไฟล์/โฟลเดอร์

    Args:
        file_id: ID ของไฟล์
        new_name: ชื่อใหม่
    """
    f = drive().files().update(
        fileId=file_id, body={"name": new_name},
        fields=FILE_FIELDS, supportsAllDrives=True,
    ).execute()
    return _fmt([f])


def get_link(file_id: str) -> str:
    """ขอลิงก์เปิดดูไฟล์ (webViewLink)

    Args:
        file_id: ID ของไฟล์
    """
    f = drive().files().get(
        fileId=file_id, fields=FILE_FIELDS, supportsAllDrives=True
    ).execute()
    return _fmt([f])


MAX_READ_CHARS = 40000

# ---------- เครื่องมือวิเคราะห์ตาราง (ไฟล์ใหญ่หลักแสนแถว) ----------

_CACHE_DIR = os.environ.get("TABLE_CACHE_DIR", os.path.join(os.path.dirname(__file__), "_cache"))

TABLE_MIMES_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
)


def _download_table(file_id: str):
    """ดาวน์โหลดไฟล์ตารางลง cache ครั้งเดียว คืน (path, ชนิด csv/xlsx, ชื่อไฟล์)"""
    meta = drive().files().get(
        fileId=file_id, fields="name, mimeType, md5Checksum, modifiedTime",
        supportsAllDrives=True,
    ).execute()
    mt, name = meta["mimeType"], meta["name"]
    ver = (meta.get("md5Checksum") or meta.get("modifiedTime", "")).replace(":", "")

    if mt == "application/vnd.google-apps.spreadsheet":
        kind = "csv"
    elif mt in TABLE_MIMES_XLSX:
        kind = "xlsx"
    elif mt.startswith("text/") or mt in ("application/csv",):
        kind = "csv"
    else:
        raise ValueError(f"ไฟล์ '{name}' ประเภท {mt} ไม่ใช่ตาราง (รองรับ Sheets/Excel/CSV)")

    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = os.path.join(_CACHE_DIR, f"{file_id}_{ver}.{kind}")
    if not os.path.exists(path):
        if mt == "application/vnd.google-apps.spreadsheet":
            try:
                raw = drive().files().export(fileId=file_id, mimeType="text/csv").execute()
                with open(path, "wb") as f:
                    f.write(raw)
            except Exception as e:  # noqa: BLE001
                if "exportSizeLimit" in str(e) or "too large" in str(e).lower():
                    _export_big_sheet_csv(file_id, path)  # Sheets ใหญ่เกิน 10MB
                else:
                    raise
        else:
            raw = drive().files().get_media(fileId=file_id).execute()
            with open(path, "wb") as f:
                f.write(raw)
    return path, kind, name


def _read_csv_flex(path: str, columns: list | None = None):
    """อ่าน CSV/TSV หลาย encoding; ท้ายสุดลองตาราง HTML (ไฟล์ .xls ปลอมจากระบบราชการ)
    ใช้ C engine (ประหยัดแรม — สำคัญบน Render 512MB) โดยเดา delimiter จากบรรทัดแรกเอง"""
    import pandas as pd

    with open(path, "rb") as f:
        head = f.read(4096)
    line = head.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    sep = max([",", "\t", ";", "|"], key=line.count)

    for enc in ("utf-8-sig", "cp874", "utf-16"):
        try:
            return pd.read_csv(path, usecols=columns or None, encoding=enc, sep=sep)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except ValueError:
            # usecols ไม่ตรง หรือ parse พัง — ลองแบบเต็มก่อนจะไป HTML
            try:
                return pd.read_csv(path, encoding=enc, sep=sep)
            except Exception:  # noqa: BLE001
                break
    return pd.read_html(path)[0]


def _clean_df(df):
    """ล้างค่าแบบ export ราชการ: ="1234" -> 1234, 'null' -> NaN, แปลงคอลัมน์เลขให้เป็นตัวเลข"""
    import pandas as pd

    df = df.replace("null", pd.NA)
    for c in df.columns:
        if df[c].dtype == object or pd.api.types.is_string_dtype(df[c]):
            df[c] = df[c].map(
                lambda v: v[2:-1] if isinstance(v, str) and v.startswith('="') and v.endswith('"') else v
            )
            non_na = df[c].dropna()
            if len(non_na):
                conv = pd.to_numeric(non_na, errors="coerce")
                if conv.notna().all():
                    df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _load_df(file_id: str, sheet: str = "", columns: list | None = None):
    import pandas as pd

    path, kind, name = _download_table(file_id)
    if kind == "xlsx":
        try:
            df = pd.read_excel(path, sheet_name=(sheet or 0), engine="calamine",
                               usecols=columns or None)
        except Exception:
            # .xls ปลอม (จริงๆ เป็น CSV หรือ HTML) — พบบ่อยในไฟล์ export จากระบบราชการ
            df = _read_csv_flex(path, columns)
    else:
        df = _read_csv_flex(path, columns)
    return _clean_df(df), name


def _apply_filter(df, filter_expr: str):
    if filter_expr:
        df = df.query(filter_expr, engine="python")
    return df


def file_stats(file_id: str, sheet: str = "") -> str:
    """ดูโครงสร้างไฟล์ตาราง: จำนวนแถว รายชื่อคอลัมน์ และตัวอย่าง 5 แถวแรก
    ใช้กับไฟล์ตารางขนาดใหญ่ก่อนเสมอ เพื่อรู้ชื่อคอลัมน์ก่อนจะ query/aggregate

    Args:
        file_id: ID ของไฟล์ (Sheets / Excel ทั้ง .xlsx และ .xls เก่า / CSV)
        sheet: ชื่อแผ่นงานใน Excel (เว้นว่าง = แผ่นแรก)
    """
    df, name = _load_df(file_id, sheet)
    cols = ", ".join(f"{c}({t})" for c, t in df.dtypes.astype(str).items())
    sample = df.head(5).to_string(index=False)
    return f"ไฟล์ '{name}': {len(df):,} แถว\nคอลัมน์: {cols}\nตัวอย่าง 5 แถวแรก:\n{sample}"


def query_file(file_id: str, filter_expr: str = "", columns: str = "",
               limit: int = 20, sheet: str = "") -> str:
    """ค้นหา/กรองแถวจากไฟล์ตารางขนาดใหญ่ คืนเฉพาะแถวที่ตรงเงื่อนไข (ไม่เกิน limit)

    Args:
        file_id: ID ของไฟล์ (Sheets/Excel/CSV)
        filter_expr: เงื่อนไขแบบ pandas query เช่น 'ราคา > 100000' หรือ
            'อำเภอ == "น้ำพอง" and ราคา >= 50000' หรือ 'ชื่อ.str.contains("แปลง")'
            (เว้นว่าง = เอาแถวแรกๆ)
        columns: รายชื่อคอลัมน์ที่ต้องการ คั่นด้วยจุลภาค (เว้นว่าง = ทุกคอลัมน์)
        limit: จำนวนแถวสูงสุดที่คืน (ค่าเริ่มต้น 20 สูงสุด 100)
        sheet: ชื่อแผ่นงานใน Excel (เว้นว่าง = แผ่นแรก)
    """
    df, name = _load_df(file_id, sheet)
    df = _apply_filter(df, filter_expr)
    total = len(df)
    if columns:
        want = [c.strip() for c in columns.split(",")]
        df = df[[c for c in want if c in df.columns]]
    out = df.head(max(1, min(int(limit), 100))).to_string(index=False)
    if len(out) > 8000:
        out = out[:8000] + "\n...(ตัดผลลัพธ์)"
    return f"พบ {total:,} แถวใน '{name}'\n{out}"


def aggregate_file(file_id: str, operation: str, column: str = "",
                   group_by: str = "", filter_expr: str = "", sheet: str = "") -> str:
    """คำนวณสรุปจากไฟล์ตารางขนาดใหญ่ (รวม เฉลี่ย นับ ต่ำสุด สูงสุด) แยกกลุ่มได้

    Args:
        file_id: ID ของไฟล์ (Sheets/Excel/CSV)
        operation: sum | mean | count | min | max | nunique
        column: คอลัมน์ตัวเลขที่จะคำนวณ (count/nunique เว้นว่างได้)
        group_by: คอลัมน์ที่ใช้แบ่งกลุ่ม เช่น 'อำเภอ' (เว้นว่าง = ทั้งไฟล์)
        filter_expr: กรองก่อนคำนวณ แบบ pandas query (เว้นว่าง = ทั้งหมด)
        sheet: ชื่อแผ่นงานใน Excel (เว้นว่าง = แผ่นแรก)
    """
    # ถ้าไม่มี filter อ่านเฉพาะคอลัมน์ที่ใช้ — ลดแรมมากสำหรับไฟล์ใหญ่
    usecols = None
    if not filter_expr:
        usecols = [c for c in {group_by, column} if c] or None
    df, name = _load_df(file_id, sheet, usecols)
    df = _apply_filter(df, filter_expr)
    op = operation.strip().lower()
    if op not in ("sum", "mean", "count", "min", "max", "nunique"):
        return "operation ต้องเป็น: sum, mean, count, min, max, nunique"

    if group_by:
        g = df.groupby(group_by)
        series = g.size() if op == "count" and not column else getattr(g[column], op)()
        series = series.sort_values(ascending=False).head(100)
        body = series.to_string()
    elif op == "count" and not column:
        body = f"{len(df):,}"
    else:
        body = str(getattr(df[column], op)())

    cond = f" (เงื่อนไข: {filter_expr})" if filter_expr else ""
    return f"{op} ของ '{name}'{cond}:\n{body}"


def read_file(file_id: str) -> str:
    """อ่านเนื้อหาไฟล์เป็นข้อความ เพื่อนำไปสรุป ตอบคำถาม หรือคำนวณตัวเลข
    รองรับ: Google Sheets, Google Docs, Excel (.xlsx), CSV, TXT

    Args:
        file_id: ID ของไฟล์ที่จะอ่าน
    """
    meta = drive().files().get(
        fileId=file_id, fields="name, mimeType", supportsAllDrives=True
    ).execute()
    mt = meta["mimeType"]
    name = meta["name"]

    if mt == "application/vnd.google-apps.spreadsheet":
        raw = drive().files().export(fileId=file_id, mimeType="text/csv").execute()
        text = raw.decode("utf-8", errors="replace")
    elif mt == "application/vnd.google-apps.document":
        raw = drive().files().export(fileId=file_id, mimeType="text/plain").execute()
        text = raw.decode("utf-8", errors="replace")
    elif mt in TABLE_MIMES_XLSX:
        df, _ = _load_df(file_id)  # รองรับ .xlsx / .xls เก่า / .xls ปลอมที่เป็น CSV
        text = f"({len(df):,} แถว)\n" + df.head(500).to_csv(index=False).rstrip()
        if len(df) > 500:
            text += "\n...(ตัดที่ 500 แถว)"
    elif mt.startswith("text/") or mt in ("application/json", "application/csv"):
        raw = drive().files().get_media(fileId=file_id).execute()
        text = raw.decode("utf-8", errors="replace")
    else:
        return f"ไฟล์ '{name}' เป็นประเภท {mt} — ยังอ่านเนื้อหาไม่ได้ (อ่านได้: Sheets, Docs, Excel, CSV, TXT)"

    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n...(ตัดเนื้อหา ไฟล์ยาวเกิน {MAX_READ_CHARS} ตัวอักษร)"
    return f"เนื้อหาไฟล์ '{name}':\n{text}"


# ---------- รูปภาพ (กราฟ / infographic) ส่งกลับเข้า LINE ----------

IMG_DIR = os.path.join(_CACHE_DIR, "img")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://line-drive-bot-e7sp.onrender.com")
_pending_images: list[str] = []


def _queue_image(png_bytes: bytes) -> str:
    """เซฟรูปแล้วคิวไว้ให้ webhook ส่งเข้า LINE หลังจบคำตอบ"""
    from uuid import uuid4

    os.makedirs(IMG_DIR, exist_ok=True)
    name = f"{uuid4().hex}.png"
    with open(os.path.join(IMG_DIR, name), "wb") as f:
        f.write(png_bytes)
    url = f"{PUBLIC_BASE_URL}/img/{name}"
    _pending_images.append(url)
    return url


def pop_pending_images() -> list[str]:
    out = _pending_images[:]
    _pending_images.clear()
    return out


def _thai_font():
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager, pyplot as plt

    fpath = os.path.join(os.path.dirname(__file__), "fonts", "Sarabun-Regular.ttf")
    if os.path.exists(fpath):
        font_manager.fontManager.addfont(fpath)
        plt.rcParams["font.family"] = "Sarabun"
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def make_chart(file_id: str, chart_type: str, group_by: str,
               value_column: str = "", operation: str = "sum",
               filter_expr: str = "", title: str = "", sheet: str = "") -> str:
    """สร้างกราฟรูปภาพจากข้อมูลในไฟล์ตาราง แล้วส่งรูปให้ผู้ใช้ใน LINE อัตโนมัติ
    ตัวเลขคำนวณจากข้อมูลจริงทั้งไฟล์ (ไฟล์ใหญ่หลักแสนแถวก็ได้)

    Args:
        file_id: ID ของไฟล์ (Sheets/Excel/CSV)
        chart_type: bar | line | pie
        group_by: คอลัมน์แกนหมวดหมู่ เช่น 'อำเภอ'
        value_column: คอลัมน์ตัวเลข (ถ้า operation=count เว้นว่างได้)
        operation: sum | mean | count | min | max (ค่าเริ่มต้น sum)
        filter_expr: กรองก่อนคำนวณ แบบ pandas query (เว้นว่าง = ทั้งหมด)
        title: ชื่อกราฟ (ภาษาไทยได้)
        sheet: ชื่อแผ่นงานใน Excel (เว้นว่าง = แผ่นแรก)
    """
    plt = _thai_font()

    df, name = _load_df(file_id, sheet)
    df = _apply_filter(df, filter_expr)
    op = operation.strip().lower()
    g = df.groupby(group_by)
    series = g.size() if (op == "count" and not value_column) else getattr(g[value_column], op)()
    series = series.sort_values(ascending=False).head(20)

    from matplotlib.ticker import FuncFormatter

    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=140)
    if chart_type == "pie":
        series.plot.pie(ax=ax, autopct="%.1f%%", ylabel="")
    elif chart_type == "line":
        series.plot.line(ax=ax, marker="o")
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    else:
        series.plot.bar(ax=ax)
        ax.grid(axis="y", alpha=0.3)
        ax.bar_label(ax.containers[0], fmt=lambda v: f"{v:,.0f}", fontsize=8)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    ax.set_title(title or f"{op} ของ {value_column or 'จำนวน'} แยกตาม {group_by}")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    _queue_image(buf.getvalue())
    return f"สร้างกราฟจาก '{name}' เรียบร้อย รูปกำลังถูกส่งในแชท (บอกผู้ใช้สั้นๆ ว่าส่งรูปแล้ว)"


def make_chart_from_data(labels: list[str], values: list[float], chart_type: str = "bar",
                         title: str = "", value_label: str = "") -> str:
    """วาดกราฟจากตัวเลขที่รวบรวม/คำนวณมาแล้ว แล้วส่งรูปให้ผู้ใช้ใน LINE
    ใช้เมื่อข้อมูลมาจากหลายไฟล์ หรือคำนวณเสร็จแล้วด้วย tool อื่น (เช่น เทียบรายสาขาข้ามไฟล์)

    Args:
        labels: ชื่อแต่ละแท่ง/จุด เช่น ["สาขา 40010000", "สาขา 40030000"]
        values: ตัวเลขตามลำดับเดียวกับ labels
        chart_type: bar | line | pie
        title: ชื่อกราฟ (ภาษาไทยได้)
        value_label: หน่วย/คำอธิบายค่า เช่น "จำนวนแปลง"
    """
    import pandas as pd
    from matplotlib.ticker import FuncFormatter

    plt = _thai_font()
    series = pd.Series(list(values), index=list(labels))

    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=140)
    if chart_type == "pie":
        series.plot.pie(ax=ax, autopct="%.1f%%", ylabel="")
    elif chart_type == "line":
        series.plot.line(ax=ax, marker="o")
        ax.grid(alpha=0.3)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    else:
        series.plot.bar(ax=ax)
        ax.grid(axis="y", alpha=0.3)
        ax.bar_label(ax.containers[0], fmt=lambda v: f"{v:,.0f}", fontsize=8)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
        plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    if value_label:
        ax.set_ylabel(value_label)
    ax.set_title(title or value_label or "กราฟสรุป")
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    _queue_image(buf.getvalue())
    return "สร้างกราฟเรียบร้อย รูปกำลังถูกส่งในแชท (บอกผู้ใช้สั้นๆ ว่าส่งรูปแล้ว)"


def make_infographic(content: str) -> str:
    """สร้างภาพ infographic สรุปเนื้อหาแบบสวยงาม (สไตล์ NotebookLM) แล้วส่งรูปให้ผู้ใช้ใน LINE
    หมายเหตุ: ข้อความไทยในภาพอาจสะกดเพี้ยนได้ และโควตาฟรีจำกัด — ตัวเลขสำคัญให้ใช้ make_chart

    Args:
        content: สรุปหัวข้อ/ประเด็น/ตัวเลขเด่นที่จะให้ปรากฏในภาพ (เขียนเป็นข้อๆ)
    """
    from google import genai

    g = genai.Client()
    try:
        resp = g.models.generate_content(
            model=os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image"),
            contents=(
                "Create a clean, modern, professional infographic poster (vertical) "
                "summarizing the following content. Minimal flat design, clear hierarchy, "
                "Thai language text rendered accurately:\n" + content
            ),
        )
    except Exception as e:  # noqa: BLE001
        if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
            return ("ยังสร้างภาพ infographic ไม่ได้: แพ็กเกจฟรีของ Gemini ไม่รวมโมเดลสร้างภาพ "
                    "(ต้องเปิด billing ใน Google AI Studio ก่อน) — ถ้าเป็นกราฟข้อมูล ใช้ make_chart แทนได้เลย")
        raise
    for part in resp.candidates[0].content.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            _queue_image(part.inline_data.data)
            return "สร้าง infographic เรียบร้อย รูปกำลังถูกส่งในแชท (บอกผู้ใช้สั้นๆ ว่าส่งรูปแล้ว)"
    return "โมเดลรูปภาพไม่คืนรูปมา ลองปรับเนื้อหาแล้วเรียกใหม่"


def ask_document(file_id: str, question: str) -> str:
    """อ่านเอกสารทั้งไฟล์แล้วตอบคำถาม/สรุป (แบบ NotebookLM) — รองรับ PDF (รวมถึง
    PDF สแกนภาษาไทย), Google Docs, Word (.docx), TXT
    ใช้กับเอกสาร/รายงาน ไม่ใช่ตารางข้อมูลขนาดใหญ่ (ตารางใช้ query_file/aggregate_file)

    Args:
        file_id: ID ของไฟล์เอกสาร
        question: สิ่งที่อยากรู้ เช่น "สรุปสาระสำคัญ" หรือคำถามเจาะจงจากเนื้อหา
    """
    from google import genai
    from google.genai import types as gtypes

    meta = drive().files().get(
        fileId=file_id, fields="name, mimeType, size", supportsAllDrives=True
    ).execute()
    mt, name = meta["mimeType"], meta["name"]

    if mt == "application/pdf":
        raw = drive().files().get_media(fileId=file_id).execute()
        part = gtypes.Part.from_bytes(data=raw, mime_type="application/pdf")
    elif mt == "application/vnd.google-apps.document":
        raw = drive().files().export(fileId=file_id, mimeType="application/pdf").execute()
        part = gtypes.Part.from_bytes(data=raw, mime_type="application/pdf")
    elif mt == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        raw = drive().files().get_media(fileId=file_id).execute()
        part = gtypes.Part.from_bytes(
            data=raw,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    elif mt.startswith("text/"):
        raw = drive().files().get_media(fileId=file_id).execute()
        part = raw.decode("utf-8", errors="replace")[:200000]
    else:
        return f"ไฟล์ '{name}' ประเภท {mt} ยังใช้ ask_document ไม่ได้ (รองรับ PDF/Docs/Word/TXT)"

    if len(raw) > 18_000_000:
        return f"ไฟล์ '{name}' ใหญ่เกิน 18MB — ยังไม่รองรับ ลองแยกไฟล์เป็นส่วนๆ"

    g = genai.Client()
    resp = g.models.generate_content(
        model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
        contents=[part, f"จากเอกสาร '{name}' นี้: {question}\nตอบเป็นภาษาไทย กระชับ อ้างอิงเนื้อหาจริงในเอกสาร"],
    )
    return (resp.text or "").strip() or "อ่านเอกสารแล้วแต่ไม่ได้คำตอบ ลองถามใหม่อีกครั้ง"


def trash_file(file_id: str) -> str:
    """ย้ายไฟล์ไปถังขยะ (กู้คืนได้ใน 30 วัน) — เรียกใช้เฉพาะเมื่อผู้ใช้ยืนยันแล้วเท่านั้น

    Args:
        file_id: ID ของไฟล์ที่จะลบ
    """
    f = drive().files().update(
        fileId=file_id, body={"trashed": True},
        fields="id, name", supportsAllDrives=True,
    ).execute()
    return f"ย้ายไปถังขยะแล้ว: {f.get('name')} ({f.get('id')})"


ALL_TOOLS = [
    search_files,
    list_folder,
    create_folder,
    move_file,
    rename_file,
    get_link,
    read_file,
    ask_document,
    file_stats,
    query_file,
    aggregate_file,
    make_chart,
    make_chart_from_data,
    make_infographic,
    trash_file,
]


def upload_bytes(data: bytes, filename: str, mime_type: str,
                 parent_id: str | None = None) -> dict:
    """อัปโหลดไฟล์ (ใช้จากฝั่ง webhook ตอนผู้ใช้ส่งไฟล์เข้ามาใน LINE)"""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    meta = {"name": filename, "parents": [parent_id or ROOT_FOLDER_ID]}
    return drive().files().create(
        body=meta, media_body=media, fields=FILE_FIELDS, supportsAllDrives=True
    ).execute()
