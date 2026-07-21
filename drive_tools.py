"""Google Drive tools สำหรับให้ Gemini เรียกใช้ (function calling)"""

import io
import json
import os
import re


def _release_memory():
    """คืนแรมที่ python ปล่อยแล้วกลับให้ OS จริงๆ — แค่ gc.collect() ไม่พอ
    เพราะ glibc malloc เก็บ arena ที่ว่างไว้เอง ไม่คืน OS เอง ต้องเรียก malloc_trim ตรงๆ
    (สำคัญมากบน Render 512MB: อ่านไฟล์ใหญ่หลายไฟล์ต่อเนื่องในคำขอเดียว ถ้าไม่ trim แรมจะสะสมพุ่งได้
    ทั้งที่แต่ละไฟล์ปล่อย reference หมดแล้ว)"""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001
        pass  # Windows/mac ไม่มี libc.so.6 (dev เครื่อง) — ข้ามไปเฉยๆ ไม่กระทบการทำงาน


def _rss_mb() -> float:
    """หน่วยความจำจริงของ process ตอนนี้ (MB) — อ่านจาก /proc (Linux เท่านั้น, ใช้ได้บน Render)"""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:  # noqa: BLE001
        pass
    return -1.0


def _instrumented(fn):
    """log เข้า/ออกของแต่ละ tool call พร้อม RSS ปัจจุบัน — ไปโผล่ใน Render Application Logs
    เอาไว้หา call สุดท้ายที่ค้าง (ไม่มีบรรทัด done ตามมา) เวลา process โดน OOM kill"""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        tag = f"{fn.__name__}({', '.join(map(repr, args))}{', ' if args and kwargs else ''}{', '.join(f'{k}={v!r}' for k, v in kwargs.items())})"
        print(f"[tool-start] rss={_rss_mb():.0f}MB {tag}", flush=True)
        try:
            result = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            print(f"[tool-error] rss={_rss_mb():.0f}MB {fn.__name__} -> {e!r}", flush=True)
            raise
        print(f"[tool-done]  rss={_rss_mb():.0f}MB {fn.__name__} -> {len(str(result))} chars", flush=True)
        return result

    return wrapper


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
    """Google Sheets ใหญ่เกินลิมิต export 10MB — ดึงผ่าน Sheets API ทีละช่วงแล้วเขียนเป็น CSV
    ไม่เคย instrument มาก่อน — เป็นจุดบอดจุดสุดท้ายที่ยังไม่เห็น log ตอน production crash กลางฟังก์ชันนี้พอดี"""
    import csv

    print(f"[sheet-export-start] rss={_rss_mb():.0f}MB file_id={file_id}", flush=True)
    meta = sheets().spreadsheets().get(
        spreadsheetId=file_id,
        fields="sheets(properties(title,gridProperties(rowCount)))",
    ).execute()
    props = meta["sheets"][0]["properties"]
    title = props["title"]
    total_rows = props["gridProperties"]["rowCount"]
    print(f"[sheet-export-meta] rss={_rss_mb():.0f}MB title={title!r} total_rows={total_rows}", flush=True)

    chunk = 20000
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        r = 1
        i = 0
        while r <= total_rows:
            rng = f"'{title}'!A{r}:AZ{min(r + chunk - 1, total_rows)}"
            vals = sheets().spreadsheets().values().get(
                spreadsheetId=file_id, range=rng,
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute().get("values", [])
            i += 1
            print(f"[sheet-export-batch] rss={_rss_mb():.0f}MB #{i} range={rng} rows_fetched={len(vals)}",
                  flush=True)
            if not vals:
                break
            w.writerows(vals)
            del vals
            r += chunk
    print(f"[sheet-export-done] rss={_rss_mb():.0f}MB file_id={file_id}", flush=True)


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


_CACHE_MAX_BYTES = 100 * 1024 * 1024  # 100MB


def _evict_cache_if_needed(directory: str, max_bytes: int = _CACHE_MAX_BYTES) -> None:
    """กันไฟล์สะสมไม่มีเพดานใน directory ที่ระบุ — บน container ที่ Render จำกัดแรมด้วย cgroup,
    page cache ของไฟล์ในดิสก์นับรวมในโควตาแรม 512MB ด้วย (ไม่ใช่แค่ heap ของ python)
    ไล่ลบไฟล์เก่าสุดก่อน (LRU) จนขนาดรวมต่ำกว่าเพดาน — ทำหน้าที่เป็น backstop เผื่อไฟล์ชั่วคราว
    ค้างจาก process ที่โดน OOM kill กลางคัน (try/finally ไม่ทันทำงาน)"""
    try:
        entries = []
        for fname in os.listdir(directory):
            p = os.path.join(directory, fname)
            if os.path.isfile(p):
                entries.append((os.path.getmtime(p), os.path.getsize(p), p))
        entries.sort()  # เก่าสุด (mtime น้อยสุด) ก่อน
        total = sum(sz for _, sz, _ in entries)
        for _, sz, p in entries:
            if total <= max_bytes:
                break
            try:
                os.remove(p)
                total -= sz
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _download_table(file_id: str):
    """ดาวน์โหลดไฟล์ตารางลงไฟล์ชั่วคราว คืน (path, ชนิด csv/xlsx, ชื่อไฟล์)
    ผู้เรียก**ต้องลบไฟล์เองหลังใช้เสร็จ** (try/finally + _safe_remove) — ไม่แคชถาวรแล้ว
    เดิมแคชค้างไว้ตามชื่อไฟล์+เวอร์ชัน ทำให้สะสมได้ถึง 165MB/24 ไฟล์ในการทดสอบ ซึ่งเสี่ยงโดน
    Render นับรวมในโควตาแรม 512MB (page cache) แม้ heap ของ python เองจะประหยัดแค่ไหนก็ตาม
    เปลี่ยนเป็นไฟล์ชั่วคราวที่มีอยู่แค่ระหว่างอ่านไฟล์นั้นๆ เท่านั้น ไม่สะสมข้ามไฟล์/ข้ามคำขอ"""
    import tempfile

    print(f"[dl-start]  rss={_rss_mb():.0f}MB file_id={file_id}", flush=True)
    meta = drive().files().get(
        fileId=file_id, fields="name, mimeType",
        supportsAllDrives=True,
    ).execute()
    mt, name = meta["mimeType"], meta["name"]
    print(f"[dl-meta]   rss={_rss_mb():.0f}MB name={name!r} mt={mt}", flush=True)

    if mt == "application/vnd.google-apps.spreadsheet":
        kind = "csv"
    elif mt in TABLE_MIMES_XLSX:
        kind = "xlsx"
    elif mt.startswith("text/") or mt in ("application/csv",):
        kind = "csv"
    else:
        raise ValueError(f"ไฟล์ '{name}' ประเภท {mt} ไม่ใช่ตาราง (รองรับ Sheets/Excel/CSV)")

    os.makedirs(_CACHE_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=f".{kind}", dir=_CACHE_DIR)
    os.close(fd)
    try:
        if mt == "application/vnd.google-apps.spreadsheet":
            try:
                raw = drive().files().export(fileId=file_id, mimeType="text/csv").execute()
                print(f"[dl-fetched] rss={_rss_mb():.0f}MB bytes={len(raw)}", flush=True)
                with open(path, "wb") as f:
                    f.write(raw)
            except Exception as e:  # noqa: BLE001
                if "exportSizeLimit" in str(e) or "too large" in str(e).lower():
                    _export_big_sheet_csv(file_id, path)  # Sheets ใหญ่เกิน 10MB
                else:
                    raise
        else:
            raw = drive().files().get_media(fileId=file_id).execute()
            print(f"[dl-fetched] rss={_rss_mb():.0f}MB bytes={len(raw)}", flush=True)
            with open(path, "wb") as f:
                f.write(raw)
    except Exception:
        _safe_remove(path)
        raise
    _evict_cache_if_needed(_CACHE_DIR)  # กวาดไฟล์ชั่วคราวเก่าที่อาจค้างจาก process ก่อนหน้า
    return path, kind, name


def _read_csv_flex(path: str, columns: list | None = None, nrows: int | None = None):
    """อ่าน CSV/TSV หลาย encoding; ท้ายสุดลองตาราง HTML (ไฟล์ .xls ปลอมจากระบบราชการ)
    ใช้ C engine (ประหยัดแรม — สำคัญบน Render 512MB) โดยเดา delimiter จากบรรทัดแรกเอง"""
    import pandas as pd

    with open(path, "rb") as f:
        head = f.read(4096)
    line = head.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    sep = max([",", "\t", ";", "|"], key=line.count)

    for enc in ("utf-8-sig", "cp874", "utf-16"):
        try:
            return pd.read_csv(path, usecols=columns or None, encoding=enc, sep=sep, nrows=nrows)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except ValueError:
            # usecols ไม่ตรง หรือ parse พัง — ลองแบบเต็มก่อนจะไป HTML
            try:
                return pd.read_csv(path, encoding=enc, sep=sep, nrows=nrows)
            except Exception:  # noqa: BLE001
                break
    df = pd.read_html(path)[0]
    return df.head(nrows) if nrows is not None else df


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
    try:
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
    finally:
        _safe_remove(path)


def _file_decodes(path: str, enc: str) -> bool:
    """เช็คว่าทั้งไฟล์ decode ด้วย encoding นี้ได้ไหม แบบ stream (ไม่โหลดทั้งไฟล์ในแรม)
    ต้อง decode ทีละก้อนด้วย incremental decoder ไม่ใช่ตัดหัวไฟล์มา decode ตรงๆ — ตัดกลาง
    ตัวอักษรไทย (UTF-8 หลายไบต์) แล้ว decode เดี่ยวๆ จะ error ทั้งที่ทั้งไฟล์จริงๆ decode ได้ปกติ"""
    import codecs

    dec = codecs.getincrementaldecoder(enc)()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(65536)
                if not buf:
                    dec.decode(b"", final=True)
                    return True
                dec.decode(buf)
    except (UnicodeDecodeError, UnicodeError):
        return False


def _iter_csv_chunks(path: str, columns: list | None = None, chunksize: int = 20000):
    """อ่าน CSV/TSV เป็นชิ้นๆ (chunksize แถวต่อครั้ง) แทนโหลดทั้งไฟล์ครั้งเดียว
    กันแรมพุ่งบนไฟล์แสนแถว (Render 512MB) — HTML table (.xls ปลอมบางแบบ) ไม่รองรับ chunk เลย fallback โหลดเต็ม"""
    import pandas as pd

    with open(path, "rb") as f:
        head = f.read(4096)
    line = head.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    sep = max([",", "\t", ";", "|"], key=line.count)
    enc = next((e for e in ("utf-8-sig", "cp874", "utf-16") if _file_decodes(path, e)), "utf-8-sig")

    i = 0
    try:
        for chunk in pd.read_csv(path, usecols=columns or None, encoding=enc, sep=sep, chunksize=chunksize):
            i += 1
            print(f"[chunk] rss={_rss_mb():.0f}MB #{i} rows={len(chunk)} cols={len(chunk.columns)} "
                  f"file={os.path.basename(path)}", flush=True)
            yield _clean_df(chunk)
        return
    except ValueError:
        try:
            for chunk in pd.read_csv(path, encoding=enc, sep=sep, chunksize=chunksize):
                i += 1
                print(f"[chunk] rss={_rss_mb():.0f}MB #{i} rows={len(chunk)} cols={len(chunk.columns)} "
                      f"file={os.path.basename(path)} (full-width fallback)", flush=True)
                yield _clean_df(chunk)
            return
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass

    df = pd.read_html(path)[0]
    if columns:
        df = df[[c for c in columns if c in df.columns]]
    yield _clean_df(df)


def _iter_table_chunks(file_id: str, sheet: str = "", columns: list | None = None, chunksize: int = 20000):
    """ให้ (chunk_df, ชื่อไฟล์) ทีละส่วน — ใช้แทน _load_df เมื่อไฟล์อาจใหญ่มาก (file_stats/query/aggregate)"""
    import pandas as pd

    path, kind, name = _download_table(file_id)
    try:
        if kind == "xlsx":
            try:
                df = pd.read_excel(path, sheet_name=(sheet or 0), engine="calamine", usecols=columns or None)
                yield _clean_df(df), name
                return
            except Exception:  # noqa: BLE001
                pass  # .xls ปลอม (จริงๆ เป็น CSV) — อ่านทาง CSV แบบ chunk ด้านล่าง
        for chunk in _iter_csv_chunks(path, columns, chunksize):
            yield chunk, name
    finally:
        _safe_remove(path)


def _peek_columns(file_id: str, sheet: str = "") -> list | None:
    """อ่านแค่หัวตาราง (0 แถว) เพื่อรู้ชื่อคอลัมน์แบบประหยัดแรม — ใช้เลือก usecols ก่อนโหลดจริง
    เมื่อโหลดไม่สำเร็จคืน None (ผู้เรียกจะ fallback ไปโหลดแบบเต็มคอลัมน์เหมือนเดิม)"""
    import pandas as pd

    path = None
    try:
        path, kind, name = _download_table(file_id)
        if kind == "xlsx":
            try:
                return list(pd.read_excel(path, sheet_name=(sheet or 0), engine="calamine", nrows=0).columns)
            except Exception:
                pass
        return list(_read_csv_flex(path, nrows=0).columns)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if path:
            _safe_remove(path)


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
    total = 0
    cols = sample = name = None
    for chunk, name in _iter_table_chunks(file_id, sheet):
        if cols is None:
            cols = ", ".join(f"{c}({t})" for c, t in chunk.dtypes.astype(str).items())
            sample = chunk.head(5).to_string(index=False)
        total += len(chunk)
    _release_memory()
    return f"ไฟล์ '{name}': {total:,} แถว\nคอลัมน์: {cols}\nตัวอย่าง 5 แถวแรก:\n{sample}"


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
    import pandas as pd

    want = [c.strip() for c in columns.split(",")] if columns else None
    limit = max(1, min(int(limit), 100))
    total = 0
    kept = []
    name = None
    for chunk, name in _iter_table_chunks(file_id, sheet):
        chunk = _apply_filter(chunk, filter_expr)
        total += len(chunk)
        if sum(len(k) for k in kept) < limit:
            piece = chunk[[c for c in want if c in chunk.columns]] if want else chunk
            kept.append(piece.head(limit - sum(len(k) for k in kept)))
    out_df = pd.concat(kept) if kept else pd.DataFrame()
    out = out_df.to_string(index=False)
    if len(out) > 8000:
        out = out[:8000] + "\n...(ตัดผลลัพธ์)"
    _release_memory()
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
    import pandas as pd

    op = operation.strip().lower()
    if op not in ("sum", "mean", "count", "min", "max", "nunique"):
        return "operation ต้องเป็น: sum, mean, count, min, max, nunique"

    # อ่านเฉพาะคอลัมน์ที่ใช้ — ลดแรมมากสำหรับไฟล์ใหญ่ (สำคัญบน Render 512MB)
    needed = {c for c in (group_by, column) if c}
    if filter_expr:
        # มี filter: ต้องรู้ว่า filter อ้างคอลัมน์ไหนด้วย — peek หัวตารางก่อน (ประหยัดกว่าโหลดเต็ม)
        header_cols = _peek_columns(file_id, sheet)
        if header_cols:
            tokens = set(re.findall(r"[A-Za-zก-๙_][A-Za-zก-๙0-9_]*", filter_expr))
            needed |= {c for c in header_cols if c in tokens}
        usecols = list(needed) if header_cols else None
    else:
        usecols = list(needed) or None

    # ประมวลผลทีละ chunk (แถวหลักหมื่นต่อครั้ง) แล้วรวมผลบางส่วนเข้าด้วยกัน
    # แทนโหลดทั้งไฟล์ครั้งเดียว — กันแรมพุ่งกับไฟล์แสนแถวคูณ 13-15 ไฟล์
    name = None
    sum_parts, cnt_parts, min_parts, max_parts, uniq_parts = [], [], [], [], []
    for chunk, name in _iter_table_chunks(file_id, sheet, usecols):
        chunk = _apply_filter(chunk, filter_expr)
        if chunk.empty:
            continue
        g = chunk.groupby(group_by) if group_by else None

        if op == "nunique":
            key = [group_by, column] if group_by else [column]
            uniq_parts.append(chunk[key].drop_duplicates())
        elif op == "count" and not column:
            cnt_parts.append(g.size() if g is not None else pd.Series([len(chunk)]))
        elif op in ("sum", "mean"):
            sum_parts.append(g[column].sum() if g is not None else pd.Series([chunk[column].sum()]))
            cnt_parts.append(g[column].count() if g is not None else pd.Series([chunk[column].count()]))
        elif op == "min":
            min_parts.append(g[column].min() if g is not None else pd.Series([chunk[column].min()]))
        elif op == "max":
            max_parts.append(g[column].max() if g is not None else pd.Series([chunk[column].max()]))
        else:  # count กับคอลัมน์ระบุ (นับที่ไม่ใช่ NaN)
            cnt_parts.append(g[column].count() if g is not None else pd.Series([chunk[column].count()]))

    def combine(parts, how):
        if not parts:
            return pd.Series(dtype="float64")
        s = pd.concat(parts)
        return getattr(s.groupby(level=0), how)() if group_by else pd.Series([getattr(s, how)()])

    if op == "nunique":
        if not uniq_parts:
            result = pd.Series(dtype="float64")
        else:
            u = pd.concat(uniq_parts).drop_duplicates()
            result = u.groupby(group_by)[column].nunique() if group_by else pd.Series([u[column].nunique()])
    elif op == "sum":
        result = combine(sum_parts, "sum")
    elif op == "mean":
        result = combine(sum_parts, "sum") / combine(cnt_parts, "sum")
    elif op == "min":
        result = combine(min_parts, "min")
    elif op == "max":
        result = combine(max_parts, "max")
    else:  # count
        result = combine(cnt_parts, "sum")

    if group_by:
        body = result.sort_values(ascending=False).head(100).to_string()
    else:
        body = str(result.iloc[0]) if len(result) else "NaN"

    cond = f" (เงื่อนไข: {filter_expr})" if filter_expr else ""
    _release_memory()
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
        import pandas as pd

        # อ่านทีละ chunk แทนโหลดทั้งไฟล์ — ไฟล์จริงมีถึง ~96,812 แถว โหลดเต็มพังแรม 512MB
        # cap 200 แถว (ลดจาก 500) — งานที่อ่านหลายไฟล์รวดในคำขอเดียว (เช่น "อ่านสรุปทั้ง 10 ไฟล์")
        # ไม่งั้นข้อความสะสมในบทสนทนาโตเกินไป
        PREVIEW_ROWS = 200
        total = 0
        head_df = None
        for chunk, _ in _iter_table_chunks(file_id):
            head_df = chunk.head(PREVIEW_ROWS) if head_df is None else pd.concat([head_df, chunk]).head(PREVIEW_ROWS)
            total += len(chunk)
        text = f"({total:,} แถว)\n" + (head_df.to_csv(index=False).rstrip() if head_df is not None else "")
        if total > PREVIEW_ROWS:
            text += f"\n...(ตัดที่ {PREVIEW_ROWS} แถว)"
    elif mt.startswith("text/") or mt in ("application/json", "application/csv"):
        raw = drive().files().get_media(fileId=file_id).execute()
        text = raw.decode("utf-8", errors="replace")
    else:
        return f"ไฟล์ '{name}' เป็นประเภท {mt} — ยังอ่านเนื้อหาไม่ได้ (อ่านได้: Sheets, Docs, Excel, CSV, TXT)"

    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n...(ตัดเนื้อหา ไฟล์ยาวเกิน {MAX_READ_CHARS} ตัวอักษร)"
    _release_memory()
    return f"เนื้อหาไฟล์ '{name}':\n{text}"


def summarize_file(file_id: str, focus: str = "") -> str:
    """อ่านไฟล์แล้วสรุปเป็นข้อความสั้นๆ ทันที (ไม่คืนข้อมูลดิบ) — ใช้แทน read_file เมื่อต้องอ่าน
    หลายไฟล์ในคำถามเดียว (เช่น "อ่านสรุปทุกไฟล์ในโฟลเดอร์") เพราะเนื้อหาดิบของแต่ละไฟล์จะถูกใช้แล้วทิ้งทันที
    ไม่ค้างอยู่ในบทสนทนาหลัก — ประหยัดทั้งแรมและ context เมื่อต้องประมวลผลหลายไฟล์ต่อเนื่อง

    Args:
        file_id: ID ของไฟล์
        focus: สิ่งที่อยากรู้จากไฟล์นี้เป็นพิเศษ (เว้นว่าง = สรุปภาพรวม)
    """
    from google import genai

    raw = read_file(file_id)
    prompt = (
        "นี่คือเนื้อหาไฟล์หนึ่งไฟล์ ช่วยสรุปสาระสำคัญให้กระชับที่สุด ไม่เกิน 4-5 บรรทัด"
        + (f" โดยเน้นเรื่อง: {focus}" if focus else "")
        + f"\n\n{raw}"
    )
    del raw
    resp = genai.Client().models.generate_content(
        model=os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        contents=prompt,
    )
    del prompt
    _release_memory()
    return (resp.text or "").strip() or "(ไฟล์นี้สรุปไม่ได้ อาจว่างเปล่า)"


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
    _evict_cache_if_needed(IMG_DIR, max_bytes=20 * 1024 * 1024)  # กราฟค้างสะสมไม่มีเพดานเหมือนกัน
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
    _instrumented(read_file),
    _instrumented(summarize_file),
    _instrumented(ask_document),
    _instrumented(file_stats),
    _instrumented(query_file),
    _instrumented(aggregate_file),
    _instrumented(make_chart),
    _instrumented(make_chart_from_data),
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
